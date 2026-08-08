# src/ai_api_unified_http/app.py

"""
FastAPI application factory.

Run locally with:
    poetry run uvicorn ai_api_unified_http.app:create_app --factory --reload
"""

from ai_api_unified.__version__ import __version__ as library_version
from fastapi import FastAPI

from .__version__ import __version__ as service_version
from .routes_v1 import router as v1_router
from .schemas import HealthResponse

API_VERSION: str = "v1"


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
