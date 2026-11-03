# tests/test_run_grants.py

"""
Run grants: a grant key opens one endpoint, on one model, up to a spend limit.

Redis is fakeredis, held on the app's state, so these tests need no server.
The completions client is a fake that charges a fixed price per turn, which
makes the spend arithmetic exact.
"""

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest
from ai_api_unified import AIFinishReason, AITokenUsage, AITurnResult
from ai_api_unified.middleware import get_observability_context
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_api_unified_http import run_grants
from ai_api_unified_http.app import create_app
from ai_api_unified_http.auth import API_KEYS_ENV, AUTH_DISABLED_ENV
from ai_api_unified_http.rate_limit import RATE_LIMIT_ENV, reset_counter
from ai_api_unified_http.run_grants import (
    RunGrantStore,
    picodollars_to_usd,
    usd_to_picodollars,
)

GRANTS: str = "/v1/run-grants"
TURN: str = "/v1/conversations/turn"
ISSUER_KEY: str = "run-service-key"
OTHER_KEY: str = "other-app-key"
TURN_COST: Decimal = Decimal("0.004")
USAGE = AITokenUsage(
    input_tokens=10, output_tokens=5, cached_input_tokens=0, total_tokens=15
)
TURN_BODY: dict[str, Any] = {
    "system_prompt": "You are terse.",
    "messages": [{"role": "user", "content": "hi"}],
}


@pytest.fixture(autouse=True)
def two_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEYS_ENV, f"runs:{ISSUER_KEY},other:{OTHER_KEY}")
    reset_counter()


@pytest.fixture
def store() -> RunGrantStore:
    return RunGrantStore(fakeredis.FakeAsyncRedis(decode_responses=True), "test:")


@pytest.fixture
def app(store: RunGrantStore) -> FastAPI:
    built: FastAPI = create_app()
    built.state.run_grant_store = store
    return built


@pytest.fixture
def seen() -> dict[str, Any]:
    """What the fake model call saw: its engine and model, and the cost context."""
    return {}


@pytest.fixture
def fake_client(seen: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.model_name = "claude-haiku-4-5"
    client.capabilities.pricing.token_rates = object()
    client.capabilities.pricing.compute_token_cost.return_value = TURN_COST

    async def turn(*args: Any, **kwargs: Any) -> AITurnResult:
        context = get_observability_context()
        seen["caller_id"] = context.caller_id
        seen["session_id"] = context.session_id
        seen["workflow_id"] = context.workflow_id
        seen["tags"] = dict(context.tags or {})
        return AITurnResult(
            text="hello",
            tool_calls=[],
            finish_reason=AIFinishReason.COMPLETE,
            raw_content=[{"type": "text", "text": "hello"}],
            usage=USAGE,
        )

    client.asend_conversation = AsyncMock(side_effect=turn)
    client.extend_messages_with_turn.side_effect = lambda messages, turn: [
        *messages,
        {"role": "assistant", "content": turn.raw_content},
    ]
    return client


@pytest.fixture
def pooled(fake_client: MagicMock) -> Iterator[MagicMock]:
    with (
        patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=fake_client,
        ) as getter,
        patch(
            "ai_api_unified_http.routes_run_grants.get_completions_client",
            return_value=fake_client,
        ),
    ):
        yield getter


def _client(app: FastAPI, key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


@pytest.fixture
def issuer(app: FastAPI) -> TestClient:
    return _client(app, ISSUER_KEY)


def _grant(issuer: TestClient, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "run_id": "run-task-a:run_harness",
        "engine": "claude",
        "spend_limit_usd": "0.50",
        **overrides,
    }
    response = issuer.post(GRANTS, json=body)
    assert response.status_code == 200, response.text
    return response.json()


class TestIssue:
    def test_issues_a_key_and_pins_the_resolved_model(
        self, issuer: TestClient, pooled: MagicMock
    ) -> None:
        grant = _grant(issuer)
        assert grant["grant_key"].startswith("rg_")
        assert grant["created"] is True
        assert grant["model"] == "claude-haiku-4-5"
        assert grant["spent_usd"] == "0"
        assert grant["spend_limit_usd"] == "0.50"

    def test_redis_keeps_only_a_hash_of_the_key(
        self, issuer: TestClient, store: RunGrantStore, pooled: MagicMock
    ) -> None:
        grant_key: str = _grant(issuer)["grant_key"]

        async def keys() -> list[str]:
            return [key async for key in store._redis.scan_iter("*")]

        dump: str = " ".join(asyncio.run(keys()))
        assert grant_key not in dump

    def test_the_same_terms_again_add_a_key_to_the_same_grant(
        self, app: FastAPI, issuer: TestClient, pooled: MagicMock
    ) -> None:
        first = _grant(issuer)
        second = _grant(issuer)
        assert second["created"] is False
        assert second["grant_key"] != first["grant_key"]
        for key in (first["grant_key"], second["grant_key"]):
            assert _client(app, key).post(TURN, json=TURN_BODY).status_code == 200

    def test_other_terms_for_a_live_run_are_refused(
        self, issuer: TestClient, pooled: MagicMock
    ) -> None:
        _grant(issuer)
        response = issuer.post(
            GRANTS,
            json={
                "run_id": "run-task-a:run_harness",
                "engine": "claude",
                "spend_limit_usd": "9.00",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"] == "run_grant_conflict"

    def test_another_issuer_cannot_take_a_live_run(
        self, app: FastAPI, issuer: TestClient, pooled: MagicMock
    ) -> None:
        _grant(issuer)
        response = _client(app, OTHER_KEY).post(
            GRANTS,
            json={
                "run_id": "run-task-a:run_harness",
                "engine": "claude",
                "spend_limit_usd": "0.50",
            },
        )
        assert response.status_code == 409

    def test_an_unpriced_model_is_refused(
        self, issuer: TestClient, fake_client: MagicMock, pooled: MagicMock
    ) -> None:
        fake_client.capabilities.pricing = None
        response = issuer.post(
            GRANTS,
            json={"run_id": "r1", "engine": "claude", "spend_limit_usd": "1"},
        )
        assert response.status_code == 400
        assert "pricing" in response.json()["detail"]

    @pytest.mark.parametrize("limit", ["0", "-1", "1e3", "abc"])
    def test_a_bad_limit_is_refused(
        self, issuer: TestClient, pooled: MagicMock, limit: str
    ) -> None:
        response = issuer.post(
            GRANTS,
            json={"run_id": "r1", "engine": "claude", "spend_limit_usd": limit},
        )
        assert response.status_code in (400, 422)

    def test_without_redis_the_endpoints_answer_503(self, pooled: MagicMock) -> None:
        bare: FastAPI = create_app()
        response = _client(bare, ISSUER_KEY).post(
            GRANTS, json={"run_id": "r1", "engine": "claude", "spend_limit_usd": "1"}
        )
        assert response.status_code == 503
        assert "HTTP_REDIS_URL" in response.json()["detail"]

    def test_an_unreachable_redis_answers_503(self, pooled: MagicMock) -> None:
        server = fakeredis.FakeServer()
        server.connected = False
        bare: FastAPI = create_app()
        bare.state.run_grant_store = RunGrantStore(
            fakeredis.FakeAsyncRedis(server=server, decode_responses=True), "test:"
        )
        response = _client(bare, ISSUER_KEY).post(
            GRANTS, json={"run_id": "r1", "engine": "claude", "spend_limit_usd": "1"}
        )
        assert response.status_code == 503
        assert response.json()["error"] == "run_grant_store_unavailable"


class TestTurn:
    def test_the_grant_decides_the_engine_and_model(
        self, app: FastAPI, issuer: TestClient, pooled: MagicMock
    ) -> None:
        grant_key: str = _grant(issuer)["grant_key"]
        response = _client(app, grant_key).post(
            TURN, json={**TURN_BODY, "engine": "openai", "model": "gpt-4o"}
        )
        assert response.status_code == 200, response.text
        engine, model = pooled.call_args.args[:2]
        assert (engine, model) == ("claude", "claude-haiku-4-5")

    def test_a_body_without_an_engine_is_fine_with_a_grant_key(
        self, app: FastAPI, issuer: TestClient, pooled: MagicMock
    ) -> None:
        grant_key: str = _grant(issuer)["grant_key"]
        response = _client(app, grant_key).post(TURN, json=TURN_BODY)
        assert response.status_code == 200
        assert response.json()["run_grant"]["spent_usd"] == "0.004"

    def test_an_api_key_turn_still_needs_an_engine(
        self, issuer: TestClient, pooled: MagicMock
    ) -> None:
        response = issuer.post(TURN, json=TURN_BODY)
        assert response.status_code == 400
        assert response.json()["detail"].startswith("engine is required")
        assert (
            issuer.post(TURN, json={**TURN_BODY, "engine": "claude"}).json()[
                "run_grant"
            ]
            is None
        )

    def test_turns_stop_once_the_run_reaches_its_limit(
        self, app: FastAPI, issuer: TestClient, pooled: MagicMock
    ) -> None:
        grant_key: str = _grant(issuer, spend_limit_usd="0.006")["grant_key"]
        sandbox = _client(app, grant_key)
        totals = [
            sandbox.post(TURN, json=TURN_BODY).json()["run_grant"]["spent_usd"]
            for _ in range(2)
        ]
        # The second turn started below the limit, so it ran and crossed it.
        assert totals == ["0.004", "0.008"]
        refused = sandbox.post(TURN, json=TURN_BODY)
        assert refused.status_code == 402
        assert refused.json()["error"] == "run_grant_spend_limit"
        assert pooled.call_count == 2

    def test_a_cache_hit_adds_nothing(
        self,
        app: FastAPI,
        issuer: TestClient,
        fake_client: MagicMock,
        pooled: MagicMock,
    ) -> None:
        grant_key: str = _grant(issuer)["grant_key"]
        hit = AITurnResult(
            text="hello",
            tool_calls=[],
            finish_reason=AIFinishReason.COMPLETE,
            raw_content=[{"type": "text", "text": "hello"}],
            usage=USAGE,
        )
        object.__setattr__(hit, "cache_hit", True)
        fake_client.asend_conversation = AsyncMock(return_value=hit)
        response = _client(app, grant_key).post(TURN, json=TURN_BODY)
        assert response.json()["run_grant"]["spent_usd"] == "0"

    def test_the_grant_attributes_the_cost_not_the_headers(
        self, app: FastAPI, issuer: TestClient, pooled: MagicMock, seen: dict
    ) -> None:
        grant_key: str = _grant(
            issuer,
            caller_id="team-7",
            session_id="session-1",
            workflow_id="run-task-a",
            tags={"profile": "pi-sample"},
        )["grant_key"]
        _client(app, grant_key).post(
            TURN,
            json=TURN_BODY,
            headers={"X-Caller-Id": "forged", "X-Workflow-Id": "forged"},
        )
        assert seen["caller_id"] == "runs:team-7"
        assert seen["workflow_id"] == "run-task-a"
        assert seen["tags"] == {
            "profile": "pi-sample",
            "run_id": "run-task-a:run_harness",
            "session_id": "session-1",
            "workflow_id": "run-task-a",
        }


class TestGrantKeyLimits:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/v1/completions"),
            ("post", GRANTS),
            ("get", f"{GRANTS}/run-task-a:run_harness"),
            ("get", "/v1/models?engine=claude"),
        ],
    )
    def test_a_grant_key_opens_only_the_turn_endpoint(
        self,
        app: FastAPI,
        issuer: TestClient,
        pooled: MagicMock,
        method: str,
        path: str,
    ) -> None:
        grant_key: str = _grant(issuer)["grant_key"]
        response = getattr(_client(app, grant_key), method)(path)
        assert response.status_code == 403

    def test_an_unknown_grant_key_is_401(self, app: FastAPI) -> None:
        response = _client(app, "rg_not-a-real-key").post(TURN, json=TURN_BODY)
        assert response.status_code == 401

    def test_an_expired_grant_key_is_401(
        self,
        app: FastAPI,
        issuer: TestClient,
        pooled: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        grant_key: str = _grant(issuer, ttl_seconds=60)["grant_key"]
        later: float = run_grants.time.time() + 120
        monkeypatch.setattr(run_grants.time, "time", lambda: later)
        assert _client(app, grant_key).post(TURN, json=TURN_BODY).status_code == 401

    def test_a_grant_key_applies_with_authentication_off(
        self,
        app: FastAPI,
        issuer: TestClient,
        pooled: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        grant_key: str = _grant(issuer, spend_limit_usd="0.001")["grant_key"]
        monkeypatch.setenv(AUTH_DISABLED_ENV, "1")
        sandbox = _client(app, grant_key)
        assert sandbox.post(TURN, json=TURN_BODY).status_code == 200
        assert sandbox.post(TURN, json=TURN_BODY).status_code == 402

    def test_each_run_has_its_own_rate_limit(
        self,
        app: FastAPI,
        issuer: TestClient,
        pooled: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        keys = [_grant(issuer, run_id=f"run-{n}")["grant_key"] for n in range(2)]
        monkeypatch.setenv(RATE_LIMIT_ENV, "1")
        reset_counter()
        first, second = (_client(app, key) for key in keys)
        assert first.post(TURN, json=TURN_BODY).status_code == 200
        assert second.post(TURN, json=TURN_BODY).status_code == 200
        assert first.post(TURN, json=TURN_BODY).status_code == 429


class TestReadAndRevoke:
    def test_get_shows_the_spend(
        self, app: FastAPI, issuer: TestClient, pooled: MagicMock
    ) -> None:
        grant_key: str = _grant(issuer)["grant_key"]
        _client(app, grant_key).post(TURN, json=TURN_BODY)
        shown = issuer.get(f"{GRANTS}/run-task-a:run_harness").json()
        assert shown["spent_usd"] == "0.004"
        assert shown["grant_key"] is None

    def test_another_issuer_sees_404(
        self, app: FastAPI, issuer: TestClient, pooled: MagicMock
    ) -> None:
        _grant(issuer)
        other = _client(app, OTHER_KEY)
        assert other.get(f"{GRANTS}/run-task-a:run_harness").status_code == 404
        assert other.delete(f"{GRANTS}/run-task-a:run_harness").status_code == 404

    def test_revoke_ends_every_key_and_reports_the_spend(
        self, app: FastAPI, issuer: TestClient, pooled: MagicMock
    ) -> None:
        keys = [_grant(issuer)["grant_key"] for _ in range(2)]
        _client(app, keys[0]).post(TURN, json=TURN_BODY)
        revoked = issuer.delete(f"{GRANTS}/run-task-a:run_harness").json()
        assert revoked == {
            "run_id": "run-task-a:run_harness",
            "revoked": True,
            "spent_usd": "0.004",
        }
        for key in keys:
            assert _client(app, key).post(TURN, json=TURN_BODY).status_code == 401
        assert issuer.get(f"{GRANTS}/run-task-a:run_harness").status_code == 404

    def test_a_second_revoke_succeeds_with_revoked_false(
        self, issuer: TestClient, pooled: MagicMock
    ) -> None:
        _grant(issuer)
        issuer.delete(f"{GRANTS}/run-task-a:run_harness")
        again = issuer.delete(f"{GRANTS}/run-task-a:run_harness")
        assert again.status_code == 200
        assert again.json()["revoked"] is False

    def test_a_revoked_run_id_can_be_granted_again(
        self, issuer: TestClient, pooled: MagicMock
    ) -> None:
        _grant(issuer)
        issuer.delete(f"{GRANTS}/run-task-a:run_harness")
        again = _grant(issuer, spend_limit_usd="2")
        assert again["created"] is True
        assert again["spent_usd"] == "0"


class TestMoney:
    @pytest.mark.parametrize(
        ("usd", "text"),
        [("0.004", "0.004"), ("0.0000001", "0.0000001"), ("12.5", "12.5")],
    )
    def test_round_trips_exactly(self, usd: str, text: str) -> None:
        assert picodollars_to_usd(usd_to_picodollars(Decimal(usd))) == text

    def test_a_fraction_of_a_picodollar_rounds_up(self) -> None:
        assert usd_to_picodollars(Decimal("0.0000000000001")) == 1
