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

from .paths import PUBLIC_PATHS
from .schemas import ErrorResponse

# Requests allowed per key per window. Zero disables the limiter.
RATE_LIMIT_ENV: Final[str] = "HTTP_RATE_LIMIT"
DEFAULT_RATE_LIMIT: Final[int] = 60

RATE_WINDOW_ENV: Final[str] = "HTTP_RATE_LIMIT_WINDOW_SECONDS"
DEFAULT_WINDOW_SECONDS: Final[int] = 60

# Whether to identify an unauthenticated caller by the forwarded header rather
# than the socket peer. Off by default: it is only correct behind a proxy that
# appends the address it saw.
CLIENT_IP_FROM_XFF_ENV: Final[str] = "HTTP_CLIENT_IP_FROM_XFF"

# Counter keys are namespaced so an address can never collide with an API key
# label, which would let one caller spend the other's budget.
_IP_KEY_PREFIX: Final[str] = "ip:"

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


def _trust_forwarded_header() -> bool:
    """Return whether the deployment sits behind a proxy that sets the header."""
    return os.environ.get(CLIENT_IP_FROM_XFF_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def client_ip(request: Request) -> str:
    """Identify a caller by address, for counting requests that carry no key.

    The socket peer is right when callers reach the service directly. Behind a
    proxy it is the proxy, which would put every caller in one bucket, so the
    forwarded header is read instead once the deployment opts in.

    Only the **last** entry of `X-Forwarded-For` is read. A proxy appends the
    address it accepted the connection from, so the final entry is the one the
    infrastructure wrote and everything before it is whatever the client sent.
    Reading the first entry, which is the usual convention, would let a caller
    prepend a value of their choosing and mint a fresh budget per request,
    which is the same as having no limit at all.

    Args:
        request: The incoming request.

    Returns:
        str: The address to count against.
    """
    if _trust_forwarded_header():
        forwarded: str = request.headers.get("x-forwarded-for", "")
        if forwarded:
            appended: str = forwarded.rsplit(",", 1)[-1].strip()
            if appended:
                return appended
    return request.client.host if request.client is not None else "unknown"


def count_unauthenticated(request: Request) -> tuple[bool, int]:
    """Count a request that presented no usable key, keyed on its address.

    Authentication runs before `RateLimitMiddleware`, because the limiter keys
    on the label that authentication resolves. A rejected request therefore
    never reaches the counter, so without this an unauthenticated caller can
    retry without bound.

    Args:
        request: The request about to be refused.

    Returns:
        tuple[bool, int]: Whether the caller is still within budget, and the
            seconds until the window resets.
    """
    limit: int = rate_limit()
    if limit == 0:
        return True, 0
    allowed, _, resets_in = _counter.hit(
        f"{_IP_KEY_PREFIX}{client_ip(request)}", limit, window_seconds()
    )
    return allowed, resets_in


def too_many_requests(limit: int, window: int, resets_in: int) -> JSONResponse:
    """Build the 429 body, shared by both paths that refuse for budget."""
    body = ErrorResponse(
        error="rate_limited",
        detail=(
            f"Over the request limit of {limit} per {window}s. Retry in "
            f"{resets_in}s, or raise {RATE_LIMIT_ENV} on the deployment."
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
            return too_many_requests(limit, window, resets_in)

        response: Response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
