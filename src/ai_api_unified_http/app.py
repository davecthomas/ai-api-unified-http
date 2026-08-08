# src/ai_api_unified_http/app.py

"""
FastAPI application factory.

Run locally with:
    make serve
    # or: poetry run uvicorn ai_api_unified_http.app:create_app --factory --reload
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from ai_api_unified.__version__ import __version__ as library_version
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .__version__ import __version__ as service_version
from .cost import attach_cost_handler, verify_cost_capture
from .errors import EXCEPTION_HANDLERS
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
    """Attach cost capture before serving, and refuse to serve without it.

    Startup order matters: the handler is attached first, then verified. The
    verification is not a formality about our own handler — it also catches a
    deployment that retuned the library's `emit_cost_topic` without telling the
    service, where capture would attach to a topic nothing publishes to.
    """
    attach_cost_handler()
    verify_cost_capture()
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type"],
    )
    app.include_router(v1_router)

    @app.get("/healthz", response_model=HealthResponse, summary="Liveness and versions")
    def healthz() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service_version=service_version,
            api_version=API_VERSION,
            library_version=library_version,
        )

    return app
