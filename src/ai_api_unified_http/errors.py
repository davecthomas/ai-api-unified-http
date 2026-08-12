# src/ai_api_unified_http/errors.py

"""
Library exception to HTTP status mapping.

The service adds no retry layer of its own — retries stay owned by the library
and the provider SDKs, so a failure arriving here is already final. The job of
this module is to classify it for the caller.

The governing distinction is whose fault the failure is. A caller who sent a
retired model or an unsupported capability gets a 4xx and can fix the request.
A provider that rate-limited or rejected *our* credentials produces a 5xx,
because the caller can do nothing about either: provider keys live only in the
service environment, so a provider auth failure is a service misconfiguration
wearing a provider's status code.

`AiProviderRequestError` carries the provider's own `status_code`, so the
classification reads it directly instead of matching on message text.
"""

import logging
import re
import uuid
from typing import Final

from ai_api_unified import (
    AiProviderCapabilityUnsupportedError,
    AiProviderRequestError,
    StructuredResponseTokenLimitError,
)
from ai_api_unified.ai_provider_exceptions import (
    AiProviderConfigurationError,
    AiProviderDependencyUnavailableError,
    AiProviderError,
)
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .schemas import ErrorResponse

logger: Final[logging.Logger] = logging.getLogger(__name__)

# Provider SDKs report missing credentials as a plain ValueError naming the
# variable, e.g. "ANTHROPIC_API_KEY environment variable must be set." The name
# is the actionable part, so it is lifted out and put in the response.
_ENV_VAR_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b([A-Z][A-Z0-9_]{4,})\b")


class ProviderNotConfiguredError(RuntimeError):
    """Raised when a provider client cannot be built from the environment.

    Distinct from `AiProviderConfigurationError`, which the library raises for
    a bad request (a retired model). This one means the deployment is missing
    configuration, so it is a 503 rather than a 4xx: the caller did nothing
    wrong and retrying the same request will not help until an operator acts.
    """

    def __init__(self, message: str, *, engine: str, missing_variable: str | None):
        super().__init__(message)
        self.engine: str = engine
        self.missing_variable: str | None = missing_variable


def missing_variable_from(error: Exception) -> str | None:
    """Extract the environment variable name a provider error complains about."""
    match = _ENV_VAR_PATTERN.search(str(error))
    return match.group(1) if match else None


# Provider statuses the service re-raises as themselves rather than remapping.
_RATE_LIMITED: Final[int] = 429
# Provider auth failures. The caller never holds provider credentials, so these
# describe a service misconfiguration and must not read as the caller's error.
_PROVIDER_AUTH_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
_BAD_GATEWAY: Final[int] = 502


def _classify_provider_status(provider_status: int | None) -> tuple[int, str]:
    """Map a provider's HTTP status onto this service's status and error code.

    Args:
        provider_status: Status the provider reported, or None when the call
            failed before it had one.

    Returns:
        tuple[int, str]: HTTP status to return, and the machine-readable
            error code for the response body.
    """
    if provider_status is None:
        # No status means the call failed before one existed: a connection
        # error or a client-side timeout. Both are upstream faults.
        return _BAD_GATEWAY, "provider_unavailable"
    if provider_status == _RATE_LIMITED:
        return _RATE_LIMITED, "provider_rate_limited"
    if provider_status in _PROVIDER_AUTH_STATUSES:
        return _BAD_GATEWAY, "provider_auth_failed"
    if provider_status >= 500:
        return _BAD_GATEWAY, "provider_error"
    # Remaining 4xx: the provider rejected the request we built from caller
    # input, so the caller can act on it (unknown model, oversized prompt).
    return 400, "provider_rejected_request"


def _status_for_request_error(error: AiProviderRequestError) -> tuple[int, str]:
    """Classify a library provider failure, which carries the provider's status."""
    return _classify_provider_status(error.status_code)


def provider_status_of(error: Exception) -> int | None:
    """Return the HTTP status a raw provider SDK exception carries, if any.

    The library wraps provider failures into its own hierarchy on the paths it
    was designed around, and does not on all of them — a bad batch id surfaces
    as `anthropic.BadRequestError`, which no handler here matches, so it would
    reach the caller as a 500 for what is plainly their own 400.

    Detection is by shape rather than by type, because the provider SDKs are
    optional extras: importing them to catch them would make this module fail
    on a deployment that installed only some of them. Every SDK in use raises
    HTTP failures as an exception carrying both a status and the response it
    came from, and that pair is specific enough not to match an ordinary bug.

    Args:
        error: The exception that escaped.

    Returns:
        int | None: The provider's HTTP status, or None when this is not a
            provider HTTP failure and should stay a 500.
    """
    status: object = getattr(error, "status_code", None)
    if not isinstance(status, int) or not hasattr(error, "response"):
        return None
    return status


def _error_response(
    status_code: int,
    error_code: str,
    detail: str,
    engine: str | None = None,
    provider_status: int | None = None,
) -> JSONResponse:
    """Build the uniform error body shared by every failure path."""
    body = ErrorResponse(
        error=error_code,
        detail=detail,
        engine=engine,
        provider_status=provider_status,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


async def handle_provider_request_error(
    request: Request, exc: Exception
) -> JSONResponse:
    """Map a provider HTTP failure onto a service response.

    Args:
        request: The failed request. Unused; required by the FastAPI handler
            signature.
        exc: The AiProviderRequestError raised by the library.

    Returns:
        JSONResponse: The classified error body.
    """
    assert isinstance(exc, AiProviderRequestError)
    status_code, error_code = _status_for_request_error(exc)
    logger.warning(
        "provider request failed: engine=%s provider_status=%s -> %s",
        exc.provider_engine,
        exc.status_code,
        status_code,
    )
    return _error_response(
        status_code=status_code,
        error_code=error_code,
        detail=str(exc),
        engine=exc.provider_engine,
        provider_status=exc.status_code,
    )


async def handle_bad_request_error(request: Request, exc: Exception) -> JSONResponse:
    """Map caller-fixable library errors onto 400.

    Covers configuration errors (a retired model, a missing engine token) and
    unsupported capabilities. Both describe a request the caller can correct.
    """
    logger.info("rejecting request: %s: %s", type(exc).__name__, exc)
    return _error_response(
        status_code=400,
        error_code="invalid_request",
        detail=str(exc),
    )


async def handle_dependency_unavailable(
    request: Request, exc: Exception
) -> JSONResponse:
    """Map a missing provider SDK onto 503.

    The deployment is missing an optional dependency, so the request is well
    formed and the service simply cannot serve it right now.
    """
    logger.error("provider dependency unavailable: %s", exc)
    return _error_response(
        status_code=503,
        error_code="provider_dependency_unavailable",
        detail=str(exc),
    )


async def handle_structured_token_limit(
    request: Request, exc: Exception
) -> JSONResponse:
    """Map a structured response truncated by the token budget onto 422.

    The caller controls `max_response_tokens`, so raising it is the fix. The
    library reports the minimum that would have worked, so the detail names it
    rather than leaving the caller to bisect.
    """
    assert isinstance(exc, StructuredResponseTokenLimitError)
    logger.info(
        "structured output exceeded token budget: model=%s sent=%s minimum=%s",
        exc.model_name,
        exc.max_response_tokens,
        exc.minimum_supported_tokens,
    )
    return _error_response(
        status_code=422,
        error_code="structured_response_token_limit",
        detail=(
            f"{exc} (sent max_response_tokens={exc.max_response_tokens}; "
            f"this model needs at least {exc.minimum_supported_tokens})"
        ),
        engine=exc.provider_name,
    )


async def handle_provider_not_configured(
    request: Request, exc: Exception
) -> JSONResponse:
    """Map a missing-credential failure onto 503 naming the variable."""
    assert isinstance(exc, ProviderNotConfiguredError)
    logger.error(
        "provider %s is not configured: %s", exc.engine, exc.missing_variable or exc
    )
    hint: str = (
        f" Set {exc.missing_variable} in the service environment."
        if exc.missing_variable
        else ""
    )
    return _error_response(
        status_code=503,
        error_code="provider_not_configured",
        detail=f"{exc}{hint}",
        engine=exc.engine,
    )


async def handle_provider_error(request: Request, exc: Exception) -> JSONResponse:
    """Map any remaining library failure onto 502.

    This is the catch-all for the `AiProviderError` hierarchy. Anything
    reaching it is an upstream failure the service could not classify further.
    """
    logger.error("unclassified provider failure: %s: %s", type(exc).__name__, exc)
    return _error_response(
        status_code=_BAD_GATEWAY,
        error_code="provider_error",
        detail=str(exc),
    )


# Ordered most specific first. FastAPI dispatches on exact exception type and
# walks the MRO, so the subclasses must be registered before their bases.
EXCEPTION_HANDLERS: Final[list[tuple[type[Exception], object]]] = [
    (ProviderNotConfiguredError, handle_provider_not_configured),
    (AiProviderRequestError, handle_provider_request_error),
    (AiProviderConfigurationError, handle_bad_request_error),
    (AiProviderCapabilityUnsupportedError, handle_bad_request_error),
    (AiProviderDependencyUnavailableError, handle_dependency_unavailable),
    (StructuredResponseTokenLimitError, handle_structured_token_limit),
    (AiProviderError, handle_provider_error),
]


class ErrorEnvelopeMiddleware(BaseHTTPMiddleware):
    """Guarantee that every failure leaves as the service's JSON error shape.

    Registered exception handlers cover the failures this service knows how to
    classify. Anything else — a library `ValueError`, a bug in a route — would
    otherwise reach Starlette's server-error handler, which sits *outside* the
    CORS middleware. The result is an HTML traceback with no
    `Access-Control-Allow-Origin` header, so a browser caller sees an opaque
    network error and cannot read the reason at all.

    This middleware runs inside CORS and outside the routes, so whatever it
    returns is still given CORS headers.

    The response body never carries the traceback. It carries a request id that
    is also written to the log line holding the traceback, so an operator can
    join the two without the caller ever seeing internals.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Run the request, converting an unhandled failure into the envelope."""
        try:
            return await call_next(request)
        except Exception as error:
            provider_status: int | None = provider_status_of(error)
            if provider_status is not None:
                status_code, error_code = _classify_provider_status(provider_status)
                logger.warning(
                    "unwrapped provider failure on %s: provider_status=%s -> %s",
                    request.url.path,
                    provider_status,
                    status_code,
                )
                return _error_response(
                    status_code=status_code,
                    error_code=error_code,
                    detail=str(error),
                    provider_status=provider_status,
                )

            request_id: str = uuid.uuid4().hex[:12]
            logger.exception(
                "unhandled error serving %s %s [request_id=%s]",
                request.method,
                request.url.path,
                request_id,
            )
            body = ErrorResponse(
                error="internal_error",
                detail=(
                    f"The service failed to handle this request. Quote "
                    f"request_id {request_id} when reporting it."
                ),
            )
            return JSONResponse(status_code=500, content=body.model_dump())
