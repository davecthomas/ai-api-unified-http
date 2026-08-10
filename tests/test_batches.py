# tests/test_batches.py

"""
Batch is the only endpoint family where the caller comes back later.

Everything else answers in one round trip. A batch is submitted, polled, and
collected across separate calls, which puts two things under test that no other
endpoint has: the handle has to survive the trip out and back, and it has to
carry enough with it to find the batch again. A batch lives in one provider's
account, so the id alone does not say where.

The rest is refusing what the provider would refuse more slowly and more
expensively.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ai_api_unified_http import rate_limit
from ai_api_unified_http.app import create_app
from ai_api_unified_http.auth import AUTH_DISABLED_ENV
from ai_api_unified_http.schemas import MAX_BATCH_REQUESTS

SUBMITTED = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_counter() -> None:
    rate_limit.reset_counter()
    yield
    rate_limit.reset_counter()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(AUTH_DISABLED_ENV, "1")
    return TestClient(create_app())


def _job(status: str = "in_progress", ended: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        batch_id="batch_abc123",
        provider_batch_id="msgbatch_xyz",
        status=SimpleNamespace(value=status),
        request_count=2,
        succeeded_count=0,
        errored_count=0,
        canceled_count=0,
        expired_count=0,
        processing_count=2,
        submitted_at_utc=SUBMITTED,
        ended_at_utc=ended,
        provider_engine="anthropic",
        provider_model_name="claude-haiku-4-5",
        provider_metadata={},
    )


def _pooled(fake: MagicMock):
    return patch(
        "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake
    )


BODY = {
    "engine": "claude",
    "requests": [
        {"custom_id": "a", "prompt": "first"},
        {"custom_id": "b", "prompt": "second"},
    ],
}


class TestSubmit:
    def test_a_batch_returns_a_handle(self, client: TestClient) -> None:
        fake = MagicMock()
        fake.submit_batch = MagicMock(return_value=_job())
        with _pooled(fake):
            response = client.post("/v1/batches", json=BODY)

        body = response.json()
        assert response.status_code == 200
        assert body["batch_id"] == "batch_abc123"
        assert body["status"] == "in_progress"
        assert body["request_count"] == 2

    def test_the_handle_carries_the_engine_back(self, client: TestClient) -> None:
        # Without this the caller holds an id they cannot use: every later call
        # needs to know which provider account holds the batch.
        fake = MagicMock()
        fake.submit_batch = MagicMock(return_value=_job())
        with _pooled(fake):
            body = client.post("/v1/batches", json=BODY).json()
        assert body["engine"] == "claude"

    def test_prompts_reach_the_library_as_batch_items(self, client: TestClient) -> None:
        fake = MagicMock()
        fake.submit_batch = MagicMock(return_value=_job())
        with _pooled(fake):
            client.post("/v1/batches", json=BODY)

        (items,), _ = fake.submit_batch.call_args
        assert [i.custom_id for i in items] == ["a", "b"]
        assert [i.prompt for i in items] == ["first", "second"]

    def test_timestamps_serialize_as_iso_strings(self, client: TestClient) -> None:
        fake = MagicMock()
        fake.submit_batch = MagicMock(return_value=_job())
        with _pooled(fake):
            body = client.post("/v1/batches", json=BODY).json()
        assert body["submitted_at_utc"] == "2026-08-10T12:00:00+00:00"
        assert body["ended_at_utc"] is None

    def test_duplicate_custom_ids_are_400_and_name_the_offenders(
        self, client: TestClient
    ) -> None:
        # The library raises ValueError for this, which would leave as a 500.
        # It is the caller's mistake and they can fix it from the message.
        fake = MagicMock()
        with _pooled(fake):
            response = client.post(
                "/v1/batches",
                json={
                    "engine": "claude",
                    "requests": [
                        {"custom_id": "dup", "prompt": "one"},
                        {"custom_id": "dup", "prompt": "two"},
                    ],
                },
            )
        assert response.status_code == 400
        assert "dup" in response.json()["detail"]
        fake.submit_batch.assert_not_called()

    def test_an_empty_batch_is_refused_without_a_provider_call(
        self, client: TestClient
    ) -> None:
        fake = MagicMock()
        with _pooled(fake):
            response = client.post(
                "/v1/batches", json={"engine": "claude", "requests": []}
            )
        assert response.status_code == 400
        fake.submit_batch.assert_not_called()

    def test_too_many_requests_is_422(self, client: TestClient) -> None:
        response = client.post(
            "/v1/batches",
            json={
                "engine": "claude",
                "requests": [
                    {"custom_id": str(n), "prompt": "x"}
                    for n in range(MAX_BATCH_REQUESTS + 1)
                ],
            },
        )
        assert response.status_code == 422


class TestPollAndCollect:
    def test_status_requires_the_engine(self, client: TestClient) -> None:
        # A batch id alone does not identify which provider holds it, so the
        # engine is a required query parameter rather than an optional one.
        response = client.get("/v1/batches/batch_abc123")
        assert response.status_code == 422

    def test_status_reports_counts(self, client: TestClient) -> None:
        fake = MagicMock()
        fake.get_batch = MagicMock(return_value=_job(status="ended", ended=SUBMITTED))
        with _pooled(fake):
            body = client.get("/v1/batches/batch_abc123?engine=claude").json()
        assert body["status"] == "ended"
        assert body["ended_at_utc"] == "2026-08-10T12:00:00+00:00"
        fake.get_batch.assert_called_once_with("batch_abc123")

    def test_results_are_keyed_by_custom_id(self, client: TestClient) -> None:
        fake = MagicMock()
        fake.get_batch_results = MagicMock(
            return_value=[
                SimpleNamespace(
                    custom_id="b",
                    status=SimpleNamespace(value="succeeded"),
                    text="second answer",
                    error_message=None,
                    provider_prompt_tokens=10,
                    provider_completion_tokens=4,
                    provider_metadata={},
                ),
                SimpleNamespace(
                    custom_id="a",
                    status=SimpleNamespace(value="errored"),
                    text=None,
                    error_message="overloaded",
                    provider_prompt_tokens=None,
                    provider_completion_tokens=None,
                    provider_metadata={},
                ),
            ]
        )
        with _pooled(fake):
            body = client.get("/v1/batches/batch_abc123/results?engine=claude").json()

        # Provider order, not request order — the ids are what correlate.
        assert [r["custom_id"] for r in body["results"]] == ["b", "a"]
        succeeded = body["results"][0]
        assert succeeded["text"] == "second answer"
        assert succeeded["usage"]["input_tokens"] == 10

    def test_a_failed_item_carries_its_reason_and_no_text(
        self, client: TestClient
    ) -> None:
        # An item can fail while the batch as a whole ends normally.
        fake = MagicMock()
        fake.get_batch_results = MagicMock(
            return_value=[
                SimpleNamespace(
                    custom_id="a",
                    status=SimpleNamespace(value="errored"),
                    text=None,
                    error_message="overloaded",
                    provider_prompt_tokens=None,
                    provider_completion_tokens=None,
                    provider_metadata={},
                )
            ]
        )
        with _pooled(fake):
            item = client.get("/v1/batches/batch_abc123/results?engine=claude").json()[
                "results"
            ][0]
        assert item["status"] == "errored"
        assert item["text"] is None
        assert item["error_message"] == "overloaded"


class TestCancel:
    def test_cancel_returns_the_canceling_state(self, client: TestClient) -> None:
        fake = MagicMock()
        fake.cancel_batch = MagicMock(return_value=_job(status="canceling"))
        with _pooled(fake):
            body = client.post("/v1/batches/batch_abc123/cancel?engine=claude").json()
        assert body["status"] == "canceling"
        fake.cancel_batch.assert_called_once_with("batch_abc123")


class TestSurface:
    def test_run_batch_is_not_exposed(self, client: TestClient) -> None:
        # It submits, polls, and blocks until the batch ends. Behind an HTTP
        # endpoint that means holding a connection open for hours and losing
        # everything if it drops.
        paths = client.get("/openapi.json").json()["paths"]
        assert not [p for p in paths if "run" in p]

    def test_every_batch_path_is_documented(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert {p for p in paths if "batches" in p} == {
            "/v1/batches",
            "/v1/batches/{batch_id}",
            "/v1/batches/{batch_id}/results",
            "/v1/batches/{batch_id}/cancel",
        }

    def test_batch_endpoints_require_a_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Auth is middleware rather than a per-route dependency precisely so a
        # new route cannot be added unprotected. This asserts that held.
        monkeypatch.delenv(AUTH_DISABLED_ENV, raising=False)
        monkeypatch.setenv("HTTP_API_KEYS", "test:a-key")
        keyed = TestClient(create_app())
        assert keyed.post("/v1/batches", json=BODY).status_code == 401
        assert keyed.get("/v1/batches/x?engine=claude").status_code == 401
