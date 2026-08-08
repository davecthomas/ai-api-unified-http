# tests/test_auth.py

"""
Authentication guards spend, so these tests care most about the ways a request
could slip past it: a missing header, a wrong key, a near-miss key, an
unlisted path, and a preflight.
"""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_api_unified_http import auth, cost
from ai_api_unified_http.app import create_app

GOOD_KEY: str = "webapp-key-value"
OTHER_KEY: str = "batch-key-value"


@pytest.fixture(autouse=True)
def configured_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Two labeled keys, plus an isolated cost sink so startup can succeed."""
    monkeypatch.setenv(auth.API_KEYS_ENV, f"webapp:{GOOD_KEY}, batch:{OTHER_KEY}")
    monkeypatch.delenv(auth.AUTH_DISABLED_ENV, raising=False)
    topic = f"test.auth.{request.node.name}"
    monkeypatch.setenv(cost.COST_TOPIC_ENV, topic)
    monkeypatch.setenv(cost.COST_LOG_PATH_ENV, str(tmp_path / "cost.jsonl"))
    yield
    logging.getLogger(topic).handlers.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_v1_request_without_a_key_is_401(client: TestClient) -> None:
    response = client.post("/v1/completions", json={"engine": "claude", "prompt": "hi"})
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_401_names_the_scheme(client: TestClient) -> None:
    # RFC 9110: a 401 tells the client how to authenticate.
    response = client.post("/v1/completions", json={"engine": "claude", "prompt": "hi"})
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_configured_key_passes_through(client: TestClient) -> None:
    # A scaffolded route keeps this about auth: 501 means the request reached
    # the handler. A live route would drag provider behavior into the check.
    response = client.post(
        "/v1/structured",
        json={"engine": "openai", "prompt": "hi", "response_schema": {}},
        headers=_auth(GOOD_KEY),
    )
    assert response.status_code == 501


def test_every_configured_key_works(client: TestClient) -> None:
    # Multiple keys exist so callers can be rotated and revoked one at a time.
    for key in (GOOD_KEY, OTHER_KEY):
        response = client.post(
            "/v1/tokens/count",
            json={"engine": "claude", "prompt": "hi"},
            headers=_auth(key),
        )
        assert response.status_code == 501


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"Authorization": "Bearer wrong-key"},
        {"Authorization": GOOD_KEY},  # missing the Bearer prefix
        {"Authorization": "Basic " + GOOD_KEY},
        {"Authorization": "Bearer "},
        {"Authorization": f"Bearer {GOOD_KEY}extra"},
        {"Authorization": f"Bearer {GOOD_KEY[:-1]}"},
    ],
)
def test_malformed_or_wrong_credentials_are_rejected(
    client: TestClient, header: dict
) -> None:
    response = client.post(
        "/v1/completions", json={"engine": "claude", "prompt": "hi"}, headers=header
    )
    assert response.status_code == 401


def test_bearer_value_is_whitespace_tolerant(client: TestClient) -> None:
    response = client.post(
        "/v1/structured",
        json={"engine": "openai", "prompt": "hi", "response_schema": {}},
        headers={"Authorization": f"Bearer  {GOOD_KEY}  "},
    )
    assert response.status_code == 501


@pytest.mark.parametrize("path", ["/healthz", "/openapi.json"])
def test_public_paths_need_no_key(client: TestClient, path: str) -> None:
    # /healthz answers load balancers that hold no credential, and the OpenAPI
    # document is what the TypeScript client is generated from.
    assert client.get(path).status_code == 200


def test_every_v1_route_is_protected(client: TestClient) -> None:
    # Auth is middleware rather than a per-route dependency precisely so that
    # adding a route cannot forget it. This asserts that property holds.
    spec = client.get("/openapi.json").json()
    v1_paths = [path for path in spec["paths"] if path.startswith("/v1")]
    assert v1_paths, "expected the v1 surface in the spec"
    for path in v1_paths:
        for method in spec["paths"][path]:
            call = getattr(client, method)
            response = call(path) if method == "get" else call(path, json={})
            assert response.status_code == 401, f"{method.upper()} {path} was not gated"


def test_cors_preflight_is_not_blocked_by_auth(client: TestClient) -> None:
    # A browser never attaches Authorization to a preflight, so gating OPTIONS
    # would make every cross-origin call fail before it could authenticate.
    response = client.options(
        "/v1/completions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_401_carries_cors_headers_so_browsers_can_read_it(client: TestClient) -> None:
    # CORS wraps auth. Without this a browser sees an opaque network failure
    # instead of the 401 body explaining what went wrong.
    response = client.post(
        "/v1/completions",
        json={"engine": "claude", "prompt": "hi"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_authorization_header_is_allowed_by_cors(client: TestClient) -> None:
    response = client.options(
        "/v1/completions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed


def test_disabled_auth_serves_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.AUTH_DISABLED_ENV, "1")
    monkeypatch.delenv(auth.API_KEYS_ENV, raising=False)
    client = TestClient(create_app())

    response = client.post(
        "/v1/structured",
        json={"engine": "openai", "prompt": "hi", "response_schema": {}},
    )
    assert response.status_code == 501


class TestKeyParsing:
    def test_labelled_and_bare_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(auth.API_KEYS_ENV, "webapp:abc, plainkey , batch:def")
        assert auth.load_api_keys() == {
            "abc": "webapp",
            "plainkey": "unnamed",
            "def": "batch",
        }

    def test_blank_entries_are_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A trailing comma must not register the empty string as a valid key,
        # which would let a caller authenticate with "Bearer ".
        monkeypatch.setenv(auth.API_KEYS_ENV, "abc,, ,")
        assert auth.load_api_keys() == {"abc": "unnamed"}

    def test_no_keys_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(auth.API_KEYS_ENV, "")
        assert auth.load_api_keys() == {}


class TestStartupGate:
    def test_startup_fails_with_no_keys_and_no_opt_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The failure this prevents: a deployment that forgot to set keys and
        # therefore serves paid endpoints to anyone who can reach the port.
        monkeypatch.delenv(auth.API_KEYS_ENV, raising=False)
        monkeypatch.delenv(auth.AUTH_DISABLED_ENV, raising=False)
        with pytest.raises(auth.AuthNotConfiguredError) as caught:
            auth.verify_auth_configured()
        assert auth.API_KEYS_ENV in str(caught.value)
        assert auth.AUTH_DISABLED_ENV in str(caught.value)

    def test_explicit_opt_out_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(auth.API_KEYS_ENV, raising=False)
        monkeypatch.setenv(auth.AUTH_DISABLED_ENV, "1")
        auth.verify_auth_configured()

    def test_configured_keys_satisfy_the_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(auth.API_KEYS_ENV, "webapp:abc")
        monkeypatch.delenv(auth.AUTH_DISABLED_ENV, raising=False)
        auth.verify_auth_configured()

    def test_app_startup_refuses_when_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(auth.API_KEYS_ENV, raising=False)
        monkeypatch.delenv(auth.AUTH_DISABLED_ENV, raising=False)
        with pytest.raises(auth.AuthNotConfiguredError), TestClient(create_app()):
            pass
