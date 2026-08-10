# src/ai_api_unified_http/request_limits.py

"""
Ceiling on the size of a request body.

The rate limiter counts requests, which bounds how *often* a caller can spend
but not how much any one call costs. A prompt is billed by the token, so a
single oversized body can cost more than the whole rest of the window, and the
limiter reports the caller as well inside their budget while it happens.

The check reads `Content-Length` and refuses before the body is read, so an
oversized request costs the service nothing beyond the headers. A request that
arrives chunked carries no declared length and passes this guard; the
per-field bounds in `schemas.py` still apply to it once parsed, and hosts that
terminate TLS in front of the service generally impose their own ceiling as
well. Cloud Run's is 32 MiB.
"""

import logging
import os
from typing import Final

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .schemas import ErrorResponse

# Largest accepted request body. Zero disables the check.
MAX_REQUEST_BYTES_ENV: Final[str] = "HTTP_MAX_REQUEST_BYTES"

# 1 MiB. Comfortably above any reasonable prompt, conversation history, or
# embeddings batch, and far below the size at which one request becomes an
# interesting way to spend someone else's provider budget.
DEFAULT_MAX_REQUEST_BYTES: Final[int] = 1_048_576

_METHODS_WITH_BODIES: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH"})

logger: Final[logging.Logger] = logging.getLogger(__name__)


def max_request_bytes() -> int:
    """Return the configured body ceiling in bytes, or 0 when disabled."""
    raw: str = os.environ.get(
        MAX_REQUEST_BYTES_ENV, str(DEFAULT_MAX_REQUEST_BYTES)
    ).strip()
    try:
        value: int = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; falling back to %s",
            MAX_REQUEST_BYTES_ENV,
            raw,
            DEFAULT_MAX_REQUEST_BYTES,
        )
        return DEFAULT_MAX_REQUEST_BYTES
    return max(value, 0)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Refuse a request whose declared body exceeds the configured ceiling."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Check the declared length, then pass the request along."""
        limit: int = max_request_bytes()
        if limit == 0 or request.method not in _METHODS_WITH_BODIES:
            return await call_next(request)

        raw: str | None = request.headers.get("content-length")
        if raw is None:
            return await call_next(request)

        try:
            declared: int = int(raw)
        except ValueError:
            return _refused(
                400,
                "invalid_request",
                f"Content-Length {raw!r} is not an integer.",
            )

        if declared > limit:
            logger.warning(
                "refused %s %s: body of %s bytes is over the %s byte limit",
                request.method,
                request.url.path,
                declared,
                limit,
            )
            return _refused(
                413,
                "request_too_large",
                (
                    f"Request body is {declared} bytes, over the limit of "
                    f"{limit}. Split the work across calls, or raise "
                    f"{MAX_REQUEST_BYTES_ENV} on the deployment."
                ),
            )

        return await call_next(request)


def _refused(status_code: int, error: str, detail: str) -> JSONResponse:
    """Build a refusal in the service-wide error shape."""
    body = ErrorResponse(error=error, detail=detail)
    return JSONResponse(status_code=status_code, content=body.model_dump())
