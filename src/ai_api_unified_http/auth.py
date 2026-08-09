# src/ai_api_unified_http/auth.py

"""
API-key authentication.

Provider credentials live only in the service environment, so every request
this service accepts spends money that the caller never had to hold a key for.
That inverts the usual risk: an unauthenticated endpoint here is not an
information leak, it is an open tab.

The scheme is a shared secret in `Authorization: Bearer <key>`. Multiple keys
are supported so callers can be revoked and rotated one at a time, and each
key carries a label used in logs to name which caller made a request.

What this deliberately is not: a user identity system. Keys name calling
applications, not people, and the service stores no per-key state beyond the
label. Anything richer belongs behind a real identity provider.
"""

import hmac
import logging
import os
from typing import Final

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .schemas import ErrorResponse

# Comma-separated keys. Each entry is either "key" or "label:key"; the label is
# for log attribution and never authenticates anything on its own.
API_KEYS_ENV: Final[str] = "HTTP_API_KEYS"

# Opt-out for local development, where the service talks to a browser app on
# localhost and requiring a key would mean pasting one into the test page.
AUTH_DISABLED_ENV: Final[str] = "HTTP_AUTH_DISABLED"

# Paths served without a key. /healthz must answer load balancers that hold no
# credential. The OpenAPI documents carry no secrets and are what the
# TypeScript client is generated from, so gating them would break codegen for
# every consumer that does not already have a key.
PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/healthz",
        "/health",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)

_BEARER_PREFIX: Final[str] = "Bearer "

logger: Final[logging.Logger] = logging.getLogger(__name__)


class AuthNotConfiguredError(RuntimeError):
    """Raised at startup when neither keys nor an explicit opt-out are set."""


def auth_disabled() -> bool:
    """Return whether the operator explicitly turned authentication off."""
    return os.environ.get(AUTH_DISABLED_ENV, "").strip().lower() in {"1", "true", "yes"}


def load_api_keys() -> dict[str, str]:
    """Parse configured keys into a key-to-label mapping.

    Returns:
        dict[str, str]: Secret to label. An unlabeled entry gets the label
            "unnamed", so log attribution always has something to print.
    """
    raw: str = os.environ.get(API_KEYS_ENV, "")
    keys: dict[str, str] = {}
    for entry in raw.split(","):
        cleaned: str = entry.strip()
        if not cleaned:
            continue
        if ":" in cleaned:
            label, _, secret = cleaned.partition(":")
            label = label.strip()
            secret = secret.strip()
        else:
            label, secret = "unnamed", cleaned
        if secret:
            keys[secret] = label or "unnamed"
    return keys


def verify_auth_configured() -> None:
    """Fail startup when the service would accept unauthenticated spend.

    Turning authentication off is allowed, but only as a deliberate act. The
    failure mode this prevents is a deployment that simply forgot to set keys
    and therefore serves paid endpoints to anyone who can reach the port.

    Raises:
        AuthNotConfiguredError: When no keys are set and no explicit opt-out
            was given.
    """
    if auth_disabled():
        logger.warning(
            "authentication is DISABLED via %s; every caller can spend "
            "provider credits through this service",
            AUTH_DISABLED_ENV,
        )
        return
    if load_api_keys():
        return
    raise AuthNotConfiguredError(
        f"No API keys are configured, so every v1 endpoint would accept "
        f"unauthenticated requests that spend provider credits. Set "
        f"{API_KEYS_ENV} to one or more comma-separated keys "
        f'(for example "webapp:$(openssl rand -hex 32)"), or set '
        f"{AUTH_DISABLED_ENV}=1 to run without authentication on purpose."
    )


def _match_key(presented: str, keys: dict[str, str]) -> str | None:
    """Return the label for a presented key, or None when it matches nothing.

    Comparison uses `hmac.compare_digest` against every configured key so the
    time taken does not depend on how far along the key matched.
    """
    matched: str | None = None
    for secret, label in keys.items():
        if hmac.compare_digest(presented, secret):
            matched = label
    return matched


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests that do not present a configured API key.

    Runs as middleware rather than a per-route dependency so a new route is
    protected by existing, and forgetting to add a dependency cannot quietly
    expose a paid endpoint.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Authenticate the request, then pass it along."""
        if auth_disabled() or request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # CORS preflight carries no Authorization header by design, so the
        # browser can never satisfy auth on an OPTIONS request.
        if request.method == "OPTIONS":
            return await call_next(request)

        header: str = request.headers.get("authorization", "")
        if not header.startswith(_BEARER_PREFIX):
            return _unauthorized("Missing bearer token.")

        presented: str = header[len(_BEARER_PREFIX) :].strip()
        label: str | None = _match_key(presented, load_api_keys())
        if label is None:
            logger.warning("rejected request to %s: unknown key", request.url.path)
            return _unauthorized("Unknown API key.")

        # Downstream handlers and logs identify the caller by label; the key
        # itself is never stored on the request or written anywhere.
        request.state.api_key_label = label
        return await call_next(request)


def _unauthorized(detail: str) -> JSONResponse:
    """Build the 401 body, matching the service-wide error shape."""
    body = ErrorResponse(error="unauthorized", detail=detail)
    return JSONResponse(
        status_code=401,
        content=body.model_dump(),
        # RFC 9110: a 401 states the scheme the client should use.
        headers={"WWW-Authenticate": "Bearer"},
    )
