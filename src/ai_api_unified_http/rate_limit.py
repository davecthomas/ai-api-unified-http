# src/ai_api_unified_http/rate_limit.py

"""
Per-key request rate limiting.

Authentication answers who may call. It does not answer how much they may
spend. Every `/v1` call bills the deployment's provider account, so a key that
leaks, or a client with a retry loop, can run up an unbounded bill against
credentials the caller never had to hold.

The limiter is a fixed-window counter kept in process memory, chosen because
of what it does not require: no Redis, no database, nothing to provision
before the service can be deployed. That choice has a consequence worth being
explicit about — the count is per process. Two workers each admit the
configured rate, so the effective limit is `HTTP_RATE_LIMIT` times the worker
count. Sizing the limit against `WEB_CONCURRENCY` is the operator's job, and
a deployment that needs an exact global ceiling needs shared state, which is
upstream work rather than something to fake here.

A fixed window also admits a burst across a boundary: a caller can spend the
tail of one window and the head of the next back to back. For a spend guard
rather than an abuse guard, that is acceptable — it bounds the hourly cost,
which is the point.
"""

import logging
import os
import threading
import time
from collections import defaultdict
from typing import Final

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .auth import PUBLIC_PATHS
from .schemas import ErrorResponse

# Requests allowed per key per window. Zero disables the limiter.
RATE_LIMIT_ENV: Final[str] = "HTTP_RATE_LIMIT"
DEFAULT_RATE_LIMIT: Final[int] = 60

RATE_WINDOW_ENV: Final[str] = "HTTP_RATE_LIMIT_WINDOW_SECONDS"
DEFAULT_WINDOW_SECONDS: Final[int] = 60

logger: Final[logging.Logger] = logging.getLogger(__name__)


def rate_limit() -> int:
    """Return the configured requests-per-window, or 0 when disabled."""
    raw: str = os.environ.get(RATE_LIMIT_ENV, str(DEFAULT_RATE_LIMIT)).strip()
    try:
        value: int = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; falling back to %s",
            RATE_LIMIT_ENV,
            raw,
            DEFAULT_RATE_LIMIT,
        )
        return DEFAULT_RATE_LIMIT
    return max(value, 0)


def window_seconds() -> int:
    """Return the window length in seconds."""
    raw: str = os.environ.get(RATE_WINDOW_ENV, str(DEFAULT_WINDOW_SECONDS)).strip()
    try:
        value: int = int(raw)
    except ValueError:
        return DEFAULT_WINDOW_SECONDS
    return max(value, 1)


class FixedWindowCounter:
    """Counts requests per caller within a window, safe across threads.

    Streaming requests are served from a threadpool, so several threads reach
    this at once and the count has to be guarded.
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._counts: dict[str, int] = defaultdict(int)
        self._window_started: float = time.monotonic()

    def hit(self, caller: str, limit: int, window: int) -> tuple[bool, int, int]:
        """Record a request and report whether it is allowed.

        Args:
            caller: Key label identifying the caller.
            limit: Requests permitted per window.
            window: Window length in seconds.

        Returns:
            tuple[bool, int, int]: Whether the request is allowed, how many
                remain in this window, and seconds until the window resets.
        """
        now: float = time.monotonic()
        with self._lock:
            elapsed: float = now - self._window_started
            if elapsed >= window:
                # Whole-window reset rather than per-caller expiry, which also
                # keeps the dict from growing without bound as keys rotate.
                self._counts.clear()
                self._window_started = now
                elapsed = 0.0

            self._counts[caller] += 1
            used: int = self._counts[caller]
            resets_in: int = max(int(window - elapsed), 1)

        allowed: bool = used <= limit
        return allowed, max(limit - used, 0), resets_in

    def reset(self) -> None:
        """Clear all counts. For tests."""
        with self._lock:
            self._counts.clear()
            self._window_started = time.monotonic()


_counter: Final[FixedWindowCounter] = FixedWindowCounter()


def reset_counter() -> None:
    """Clear the process-wide counter. For tests."""
    _counter.reset()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Reject a caller who exceeds their request budget for the window.

    Runs after authentication so the counter keys on the API key's label
    rather than an IP address, which a shared NAT or a proxy would make
    meaningless.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Count the request, or refuse it with a 429."""
        limit: int = rate_limit()
        if limit == 0 or request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)

        # Set by the auth middleware. Unauthenticated requests never reach
        # here, so the fallback only covers a deployment running with
        # authentication disabled, where every caller shares one bucket.
        caller: str = getattr(request.state, "api_key_label", "anonymous")
        window: int = window_seconds()
        allowed, remaining, resets_in = _counter.hit(caller, limit, window)

        if not allowed:
            logger.warning(
                "rate limit exceeded for caller %r: over %s per %ss",
                caller,
                limit,
                window,
            )
            body = ErrorResponse(
                error="rate_limited",
                detail=(
                    f"Over the request limit of {limit} per {window}s for this "
                    f"API key. Retry in {resets_in}s, or raise "
                    f"{RATE_LIMIT_ENV} on the deployment."
                ),
            )
            return JSONResponse(
                status_code=429,
                content=body.model_dump(),
                headers={
                    "Retry-After": str(resets_in),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response: Response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
