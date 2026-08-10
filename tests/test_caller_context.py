# tests/test_caller_context.py

"""
Cost attribution below the API key.

A key identifies a calling application, so a web app serving many users holds
one key and produces one undifferentiated spend total. These tests cover the
identifiers that split it, the namespacing that keeps two applications' ids
apart, and the sanitizing that stops a caller writing whatever it likes into
the cost record.
"""

from unittest.mock import MagicMock, patch

import pytest
from ai_api_unified.middleware import get_observability_context
from fastapi.testclient import TestClient

from ai_api_unified_http import caller_context
from ai_api_unified_http.app import create_app
from ai_api_unified_http.auth import API_KEYS_ENV
from ai_api_unified_http.caller_context import (
    CALLER_ID_HEADER,
    MAX_IDENTIFIER_LENGTH,
    SESSION_ID_HEADER,
    WORKFLOW_ID_HEADER,
    namespaced_caller,
    sanitize_identifier,
)

KEY: str = "webapp-key"
PATH: str = "/v1/tokens/count"
BODY: dict = {"engine": "claude", "prompt": "hi"}


@pytest.fixture
def seen() -> dict:
    """Capture the observability context visible inside the route."""
    return {}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, seen: dict) -> TestClient:
    monkeypatch.setenv(API_KEYS_ENV, f"webapp:{KEY}")

    fake = MagicMock()

    def record(prompt: str) -> int:
        # Read the context from inside the call the library would make, which
        # is the only place that proves the identifiers actually reach it.
        context = get_observability_context()
        seen["caller_id"] = context.caller_id
        seen["session_id"] = context.session_id
        seen["workflow_id"] = context.workflow_id
        seen["tags"] = dict(context.tags or {})
        return 7

    fake.count_tokens = MagicMock(side_effect=record)
    with patch(
        "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake
    ):
        yield TestClient(create_app())


def _post(client: TestClient, headers: dict | None = None):
    merged = {"Authorization": f"Bearer {KEY}"}
    merged.update(headers or {})
    return client.post(PATH, json=BODY, headers=merged)


class TestIdentifiersReachTheLibrary:
    def test_caller_id_is_namespaced_by_the_api_key_label(
        self, client: TestClient, seen: dict
    ) -> None:
        assert _post(client, {CALLER_ID_HEADER: "user-42"}).status_code == 200
        assert seen["caller_id"] == "webapp:user-42"

    def test_session_and_workflow_also_travel_as_tags(
        self, client: TestClient, seen: dict
    ) -> None:
        # The library's cost event carries a fixed field set: caller_id is in
        # it, session_id and workflow_id are not. Tags are emitted on cost
        # events, so the session is only visible in the billing record when it
        # is sent as one.
        _post(
            client,
            {
                CALLER_ID_HEADER: "user-42",
                SESSION_ID_HEADER: "sess-1",
                WORKFLOW_ID_HEADER: "wf-9",
            },
        )
        assert seen["tags"]["session_id"] == "sess-1"
        assert seen["tags"]["workflow_id"] == "wf-9"

    def test_session_and_workflow_pass_through_unnamespaced(
        self, client: TestClient, seen: dict
    ) -> None:
        # Only the caller id needs the key's namespace; a session or workflow
        # id is already the caller's own opaque value.
        _post(
            client,
            {
                CALLER_ID_HEADER: "user-42",
                SESSION_ID_HEADER: "sess-1",
                WORKFLOW_ID_HEADER: "wf-9",
            },
        )
        assert seen["session_id"] == "sess-1"
        assert seen["workflow_id"] == "wf-9"

    def test_without_a_caller_header_spend_attributes_to_the_application(
        self, client: TestClient, seen: dict
    ) -> None:
        # The key label alone, so the cost record still says which application
        # spent the money rather than falling back to a shared default.
        _post(client)
        assert seen["caller_id"] == "webapp"

    def test_the_context_does_not_leak_between_requests(
        self, client: TestClient, seen: dict
    ) -> None:
        # The context lives in a contextvar, and a worker thread reused for the
        # next request would otherwise carry the previous caller's identity
        # into someone else's cost record.
        _post(client, {CALLER_ID_HEADER: "user-42", SESSION_ID_HEADER: "sess-1"})
        assert seen["caller_id"] == "webapp:user-42"

        _post(client)
        assert seen["caller_id"] == "webapp"
        assert seen["session_id"] is None


class TestSanitizing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("user-42", "user-42"),
            ("  user-42  ", "user-42"),
            ("", None),
            ("   ", None),
            (None, None),
        ],
    )
    def test_trimming_and_blanks(self, raw: str | None, expected: str | None) -> None:
        assert sanitize_identifier(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "user\n2026-01-01 INFO forged log line",
            "user\r\nfake: entry",
            "user\x00null",
            "user\x1bescape",
        ],
    )
    def test_control_characters_are_stripped(self, raw: str) -> None:
        # The value lands in log lines and cost events, so a newline would let
        # a caller forge a record.
        cleaned = sanitize_identifier(raw)
        assert cleaned is not None
        assert "\n" not in cleaned
        assert "\r" not in cleaned
        assert "\x00" not in cleaned
        assert "\x1b" not in cleaned

    def test_length_is_bounded(self) -> None:
        cleaned = sanitize_identifier("u" * (MAX_IDENTIFIER_LENGTH * 3))
        assert cleaned is not None
        assert len(cleaned) == MAX_IDENTIFIER_LENGTH

    def test_a_forged_newline_cannot_reach_the_context(
        self, client: TestClient, seen: dict
    ) -> None:
        _post(client, {CALLER_ID_HEADER: "user-42 fake-entry"})
        assert "\n" not in seen["caller_id"]


class TestNamespacing:
    def test_two_applications_numbering_from_one_do_not_collide(self) -> None:
        # Both send "user-1"; the cost record must keep them apart, or the
        # total lands on whichever application the reader assumed.
        assert namespaced_caller("webapp", "user-1") != namespaced_caller(
            "batch", "user-1"
        )

    def test_a_missing_caller_id_yields_the_label_alone(self) -> None:
        assert namespaced_caller("webapp", None) == "webapp"


def test_identifiers_survive_the_threadpool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """count_tokens runs in the threadpool, and the context has to follow it.

    The sync library calls are the ones that emit cost events for token
    counting and model listing, so a context that stopped at the event loop
    would leave exactly those events unattributed.
    """
    monkeypatch.setenv(API_KEYS_ENV, f"webapp:{KEY}")
    observed: dict = {}

    fake = MagicMock()

    def in_thread(prompt: str) -> int:
        import threading

        observed["thread"] = threading.current_thread().name
        observed["caller_id"] = get_observability_context().caller_id
        return 5

    fake.count_tokens = MagicMock(side_effect=in_thread)
    with patch(
        "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake
    ):
        client = TestClient(create_app())
        client.post(
            PATH,
            json=BODY,
            headers={"Authorization": f"Bearer {KEY}", CALLER_ID_HEADER: "user-7"},
        )

    assert observed["caller_id"] == "webapp:user-7"
    assert observed["thread"] != "MainThread"


def test_header_names_are_the_documented_ones() -> None:
    # Renaming these silently would break every caller's attribution while
    # every request kept returning 200.
    assert caller_context.CALLER_ID_HEADER == "X-Caller-Id"
    assert caller_context.SESSION_ID_HEADER == "X-Session-Id"
    assert caller_context.WORKFLOW_ID_HEADER == "X-Workflow-Id"
