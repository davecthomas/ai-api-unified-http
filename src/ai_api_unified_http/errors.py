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

from .schemas import ErrorResponse

logger: Final[logging.Logger] = logging.getLogger(__name__)

# Provider statuses the service re-raises as themselves rather than remapping.
_RATE_LIMITED: Final[int] = 429
# Provider auth failures. The caller never holds provider credentials, so these
# describe a service misconfiguration and must not read as the caller's error.
_PROVIDER_AUTH_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
_BAD_GATEWAY: Final[int] = 502


def _status_for_request_error(error: AiProviderRequestError) -> tuple[int, str]:
    """Classify a provider HTTP failure into a service status and error code.

    Args:
        error: The provider request failure, carrying the provider's status.

    Returns:
        tuple[int, str]: HTTP status to return, and the machine-readable
            error code for the response body.
    """
    provider_status: int | None = error.status_code

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
    (AiProviderRequestError, handle_provider_request_error),
    (AiProviderConfigurationError, handle_bad_request_error),
    (AiProviderCapabilityUnsupportedError, handle_bad_request_error),
    (AiProviderDependencyUnavailableError, handle_dependency_unavailable),
    (StructuredResponseTokenLimitError, handle_structured_token_limit),
    (AiProviderError, handle_provider_error),
]
