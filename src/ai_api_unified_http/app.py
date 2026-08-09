# src/ai_api_unified_http/app.py

"""
FastAPI application factory.

Run locally with:
    make serve
    # or: poetry run uvicorn ai_api_unified_http.app:create_app --factory --reload
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from ai_api_unified.__version__ import __version__ as library_version
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .__version__ import __version__ as service_version
from .auth import ApiKeyAuthMiddleware, verify_auth_configured
from .config import load_env_file
from .cost import (
    apply_default_middleware_config,
    attach_cost_handler,
    verify_cost_capture,
)
from .errors import EXCEPTION_HANDLERS, ErrorEnvelopeMiddleware
from .logging_setup import configure_logging
from .rate_limit import RateLimitMiddleware, rate_limit, window_seconds
from .routes_v1 import router as v1_router
from .schemas import HealthResponse

API_VERSION: str = "v1"

# Browser callers need CORS. Default covers the local test web app
# (make webapp); deployments override with their web apps' origins.
DEFAULT_CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
CORS_ORIGINS_ENV: str = "HTTP_CORS_ORIGINS"


def _cors_origins() -> list[str]:
    """Comma-separated origins from HTTP_CORS_ORIGINS, else the local default."""
    raw: str = os.environ.get(CORS_ORIGINS_ENV, DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Verify the service can account for and gate its own spend before serving.

    Both checks guard the same thing from opposite sides: cost capture records
    what was spent, and authentication governs who can spend it. A deployment
    missing either one is one this service refuses to run.

    Startup order matters for the first: the handler is attached, then
    verified. That verification also catches a deployment that retuned the
    library's `emit_cost_topic` without telling the service, where capture
    would attach to a topic nothing publishes to.
    """
    # The env file first: everything below reads configuration, and the
    # library resolves its settings from os.environ, which nothing else
    # populates from the file.
    load_env_file()

    # Logging next, so every decision below is visible in the log rather than
    # discarded by an unconfigured root logger.
    level: int = configure_logging()
    logging.getLogger(__name__).info(
        "starting ai-api-unified-http %s (library %s, log level %s)",
        service_version,
        library_version,
        logging.getLevelName(level),
    )

    # The middleware profile has to be resolved first: it decides both whether
    # the library emits cost events and which topic it publishes them on.
    apply_default_middleware_config()
    attach_cost_handler()
    verify_cost_capture()
    verify_auth_configured()

    limit: int = rate_limit()
    if limit:
        logging.getLogger(__name__).info(
            "rate limit: %s requests per %ss per key, per worker",
            limit,
            window_seconds(),
        )
    else:
        logging.getLogger(__name__).warning(
            "rate limiting is DISABLED; a caller can spend without bound"
        )
    yield


def create_app() -> FastAPI:
    """Build the FastAPI app with the v1 router and health endpoint."""
    app = FastAPI(
        title="ai-api-unified-http",
        version=service_version,
        description=(
            "HTTP interface to the ai-api-unified Python library. "
            "Breaking API changes bump the URI version (/v1 -> /v2); the "
            "service itself follows semantic versioning independently."
        ),
        lifespan=lifespan,
    )
    for exception_type, handler in EXCEPTION_HANDLERS:
        app.add_exception_handler(exception_type, handler)
    # Starlette runs the last-added middleware outermost, so CORS is added
    # after auth in order to wrap it. A 401 raised by auth has to carry CORS
    # headers, or a browser caller sees an opaque network error instead of the
    # 401 body telling it the key was missing.
    # Nesting, outermost first: CORS, error envelope, auth, rate limit, routes.
    # add_middleware prepends, so these are registered inside-out. The envelope
    # sits inside CORS so its responses still carry CORS headers, and outside
    # auth so a bug there is enveloped too. The limiter sits inside auth
    # because it counts against the key's label, which auth resolves.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(ApiKeyAuthMiddleware)
    app.add_middleware(ErrorEnvelopeMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type", "authorization"],
    )
    app.include_router(v1_router)

    def _health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service_version=service_version,
            api_version=API_VERSION,
            library_version=library_version,
        )

    # Served at two paths for one reason: Google Cloud Run's frontend answers
    # /healthz itself and never forwards it to the container, so a deployment
    # there needs a path it does not reserve. Both return the same body.
    app.get("/healthz", response_model=HealthResponse, summary="Liveness and versions")(
        _health
    )
    app.get("/health", response_model=HealthResponse, summary="Liveness and versions")(
        _health
    )

    return app
