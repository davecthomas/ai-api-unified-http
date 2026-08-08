# tests/test_cors.py

"""
The browser test app (make webapp, port 3000) calls the service cross-origin,
so the default CORS config must admit the local web app origins and reject
others. Deployments override via HTTP_CORS_ORIGINS.
"""

import pytest
from fastapi.testclient import TestClient

from ai_api_unified_http.app import create_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


def test_local_webapp_origin_allowed(client: TestClient) -> None:
    response = client.get("/healthz", headers={"origin": "http://localhost:3000"})
    assert response.status_code == 200
    assert (
        response.headers.get("access-control-allow-origin") == "http://localhost:3000"
    )


def test_preflight_allows_post_with_json(client: TestClient) -> None:
    response = client.options(
        "/v1/completions",
        headers={
            "origin": "http://localhost:3000",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert "POST" in response.headers.get("access-control-allow-methods", "")


def test_unknown_origin_gets_no_cors_headers(client: TestClient) -> None:
    response = client.get("/healthz", headers={"origin": "https://evil.example.com"})
    # The request still succeeds (CORS is a browser contract, not auth), but
    # no allow-origin header is granted, so a browser will block the read.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_origins_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_CORS_ORIGINS", "https://app.example.com")
    client = TestClient(create_app())
    response = client.get("/healthz", headers={"origin": "https://app.example.com"})
    assert (
        response.headers.get("access-control-allow-origin") == "https://app.example.com"
    )
    response = client.get("/healthz", headers={"origin": "http://localhost:3000"})
    assert "access-control-allow-origin" not in response.headers
