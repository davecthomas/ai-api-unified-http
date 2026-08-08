# tests/test_endpoints.py

"""
Bootstrap contract: /healthz is live and reports all three versions; every
scaffolded v1 endpoint returns 501 with the uniform not-implemented body,
and the OpenAPI spec includes the full v1 surface.
"""

import pytest
from fastapi.testclient import TestClient


def test_healthz_reports_versions(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["api_version"] == "v1"
    # Exact values are asserted by test_version_sync; here just shape.
    assert body["service_version"].count(".") == 2
    assert body["library_version"].count(".") == 2


SCAFFOLDED_POST_ENDPOINTS: list[tuple[str, dict]] = [
    (
        "/v1/structured",
        {"engine": "openai", "prompt": "hi", "response_schema": {"type": "object"}},
    ),
    (
        "/v1/conversations/turn",
        {"engine": "claude", "system_prompt": "s", "messages": []},
    ),
    ("/v1/embeddings", {"engine": "google-gemini", "inputs": ["hi"]}),
    ("/v1/tokens/count", {"engine": "claude", "prompt": "hi"}),
]


@pytest.mark.parametrize("path,body", SCAFFOLDED_POST_ENDPOINTS)
def test_post_endpoints_return_501(client: TestClient, path: str, body: dict) -> None:
    response = client.post(path, json=body)
    assert response.status_code == 501
    payload = response.json()
    assert payload["error"] == "not_implemented"
    assert payload["endpoint"] == path


def test_models_returns_501(client: TestClient) -> None:
    response = client.get("/v1/models")
    assert response.status_code == 501
    assert response.json()["error"] == "not_implemented"


def test_invalid_body_is_422(client: TestClient) -> None:
    # Schema validation runs before any handler, so the OpenAPI request
    # shapes are enforced regardless of whether the route is live.
    response = client.post("/v1/completions", json={"prompt": "no engine"})
    assert response.status_code == 422


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
