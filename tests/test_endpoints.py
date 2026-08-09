# tests/test_endpoints.py

"""
Service-wide surface contract: /healthz reports all three versions, request
schemas are enforced before any handler runs, and the OpenAPI spec carries the
full v1 surface the TypeScript client is generated from.

Per-endpoint behavior lives in the endpoint's own test module.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("path", ["/healthz", "/health"])
def test_health_reports_versions(client: TestClient, path: str) -> None:
    # Two paths because Cloud Run's frontend answers /healthz itself and never
    # forwards it to the container, so a deployment there needs /health.
    response = client.get(path)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["api_version"] == "v1"
    # Exact values are asserted by test_version_sync; here just shape.
    assert body["service_version"].count(".") == 2
    assert body["library_version"].count(".") == 2


def test_invalid_body_is_422(client: TestClient) -> None:
    # Schema validation runs before any handler, so a malformed request never
    # reaches a provider and never costs anything.
    response = client.post("/v1/completions", json={"prompt": "no engine"})
    assert response.status_code == 422


def test_models_requires_an_engine(client: TestClient) -> None:
    # engine is required rather than optional: listing every engine would
    # construct a client per engine on one request.
    assert client.get("/v1/models").status_code == 422


def test_openapi_covers_v1_surface(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    expected_paths = {
        "/v1/completions",
        "/v1/structured",
        "/v1/conversations/turn",
        "/v1/embeddings",
        "/v1/tokens/count",
        "/v1/models",
        "/healthz",
    }
    assert expected_paths.issubset(spec["paths"].keys())


def test_no_endpoint_still_returns_not_implemented(client: TestClient) -> None:
    # The whole documented v1 surface is live. If a route regresses to a 501
    # scaffold, this catches it rather than the README quietly going stale.
    spec = client.get("/openapi.json").json()
    for path, methods in spec["paths"].items():
        if not path.startswith("/v1"):
            continue
        for operation in methods.values():
            assert "501" not in operation.get("responses", {}), path
