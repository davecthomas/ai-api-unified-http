# tests/test_admin_cache.py
"""
The operator endpoints for the response cache: a separate admin key opens
them, a client key does not, and they report and clear the library's cache
as this process sees it.
"""

from typing import Any

import pytest
from ai_api_unified.ai_base import AIBaseCompletions, AICompletionsCapabilitiesBase
from fastapi.testclient import TestClient

from ai_api_unified_http import auth
from ai_api_unified_http.app import create_app

ADMIN_KEY: str = "test-admin-key"
PATH: str = "/v1/admin/cache"


class _Engine(AIBaseCompletions):
    """A completions engine the library's cache wraps, with no provider."""

    def __init__(self) -> None:
        super().__init__(model="fake-model")
        self.calls: int = 0

    @property
    def capabilities(self) -> AICompletionsCapabilitiesBase:
        return AICompletionsCapabilitiesBase(context_window_length=1000)

    @property
    def list_model_names(self) -> list[str]:
        return ["fake-model"]

    @property
    def max_context_tokens(self) -> int:
        return 1000

    def send_prompt(self, prompt: str, **kwargs: Any) -> str:
        self.calls += 1
        return f"answer to {prompt}"

    def strict_schema_prompt(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.ADMIN_KEYS_ENV, f"ops:{ADMIN_KEY}")


@pytest.fixture
def admin_client(admin_env: None) -> TestClient:
    return TestClient(create_app(), headers={"Authorization": f"Bearer {ADMIN_KEY}"})


class TestAuth:
    def test_off_when_no_admin_keys_are_set(self, client: TestClient) -> None:
        response = client.get(PATH)
        assert response.status_code == 403
        assert auth.ADMIN_KEYS_ENV in response.json()["detail"]

    def test_a_client_key_does_not_open_admin(
        self, admin_env: None, client: TestClient
    ) -> None:
        assert client.get(PATH).status_code == 401

    def test_an_admin_key_does_not_open_client_routes(
        self, admin_client: TestClient
    ) -> None:
        assert admin_client.post("/v1/tokens/count", json={}).status_code == 401

    def test_no_key_is_401(self, admin_env: None, anonymous_client: TestClient) -> None:
        assert anonymous_client.get(PATH).status_code == 401

    def test_admin_key_opens_admin(self, admin_client: TestClient) -> None:
        assert admin_client.get(PATH).status_code == 200


class TestCacheView:
    def test_reports_entries_and_counters_without_content(
        self, admin_client: TestClient
    ) -> None:
        engine = _Engine()
        engine.send_prompt("a private question")
        engine.send_prompt("a private question")
        engine.send_prompt("another")
        assert engine.calls == 2

        body = admin_client.get(PATH).json()
        stats = body["stats"]
        assert stats["enabled"] is True and stats["store"] == "InMemoryCacheStore"
        assert stats["entries"] == 2 and stats["hits"] == 1 and stats["misses"] == 2
        assert stats["by_operation"]["completions"]["hits"] == 1
        assert len(body["entries"]) == 2
        entry = body["entries"][0]
        assert entry["operation"] == "completions"
        # The fake meters nothing, so the entry names no serving model; a real
        # engine's entry carries the model its observed call reported.
        assert entry["model"] is None
        assert entry["expires_in_seconds"] > 0
        assert "private" not in response_text(body)

    def test_limit_caps_the_listing(self, admin_client: TestClient) -> None:
        engine = _Engine()
        for int_index in range(3):
            engine.send_prompt(f"q{int_index}")
        assert len(admin_client.get(PATH, params={"limit": 2}).json()["entries"]) == 2
        assert admin_client.get(PATH, params={"limit": 0}).status_code == 422

    def test_delete_clears_and_keeps_counters(self, admin_client: TestClient) -> None:
        engine = _Engine()
        engine.send_prompt("q")
        engine.send_prompt("q")
        cleared = admin_client.delete(PATH, params={"operation": "embeddings"}).json()
        assert cleared == {"removed": 0, "operation": "embeddings"}
        cleared = admin_client.delete(PATH).json()
        assert cleared == {"removed": 1, "operation": None}
        stats = admin_client.get(PATH).json()["stats"]
        assert stats["entries"] == 0 and stats["hits"] == 1
        engine.send_prompt("q")
        assert engine.calls == 2

    def test_shipped_profile_enables_the_cache(self, admin_client: TestClient) -> None:
        assert admin_client.get(PATH).json()["stats"]["enabled"] is True


def response_text(body: dict) -> str:
    import json

    return json.dumps(body)
