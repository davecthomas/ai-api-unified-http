# tests/test_attachments.py

"""
Non-text inputs on a completion.

Two things are worth being careful about here, and neither is the happy path.

The library decides which attachment types a model will take, and today that is
images alone even though its data-type vocabulary names five. The service must
not hold its own copy of that policy: it classifies the MIME type, hands the
attachment over, and translates the refusal. Then the day the library accepts
audio, this endpoint does too with no change — and a test proves the refusal
comes from the library rather than from a list here.

The other is that an attachment can name a stored artifact instead of carrying
bytes, which turns an artifact id into a read. It has to be scoped to the
caller who owns it, exactly as fetching that artifact directly is.
"""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ai_api_unified_http import artifacts, rate_limit
from ai_api_unified_http.app import create_app
from ai_api_unified_http.auth import API_KEYS_ENV
from ai_api_unified_http.schemas import MAX_ATTACHMENTS

FIRST_KEY: str = "first-caller-key"
SECOND_KEY: str = "second-caller-key"
PATH: str = "/v1/completions"
PNG: bytes = b"\x89PNG\r\n\x1a\n" + b"pixels" * 64
PNG_B64: str = base64.b64encode(PNG).decode()


@pytest.fixture(autouse=True)
def clean_counter() -> None:
    rate_limit.reset_counter()
    yield
    rate_limit.reset_counter()


@pytest.fixture
def store(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(artifacts.ARTIFACT_DIR_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_client() -> MagicMock:
    fake = MagicMock()
    fake.asend_prompt = AsyncMock(return_value="a red bicycle")
    fake.send_prompt_streaming = MagicMock(return_value=iter(["a ", "bicycle"]))
    return fake


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, store) -> TestClient:
    monkeypatch.setenv(API_KEYS_ENV, f"first:{FIRST_KEY},second:{SECOND_KEY}")
    return TestClient(create_app())


def _auth(key: str = FIRST_KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _pooled(fake: MagicMock):
    return patch(
        "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake
    )


def _params_of(mock_call) -> object:
    return mock_call.kwargs["other_params"]


class TestAttachmentsReachTheProvider:
    def test_an_image_arrives_as_bytes_not_base64(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        with _pooled(fake_client):
            response = client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "what is this?",
                    "attachments": [{"mime_type": "image/png", "data": PNG_B64}],
                },
                headers=_auth(),
            )
        assert response.status_code == 200
        params = _params_of(fake_client.asend_prompt.await_args)
        assert params.included_data == [PNG]
        assert params.included_mime_types == ["image/png"]

    def test_the_three_lists_stay_aligned(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        # The library pairs them by index, so a mismatch would attach the wrong
        # type to the wrong bytes rather than failing loudly.
        with _pooled(fake_client):
            client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "compare these",
                    "attachments": [
                        {"mime_type": "image/png", "data": PNG_B64},
                        {"mime_type": "image/jpeg", "data": PNG_B64},
                    ],
                },
                headers=_auth(),
            )
        params = _params_of(fake_client.asend_prompt.await_args)
        assert len(params.included_types) == 2
        assert len(params.included_data) == 2
        assert params.included_mime_types == ["image/png", "image/jpeg"]

    def test_attachments_work_on_the_streaming_path_too(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        # Both library calls take the same parameters object. This is the half
        # that would be easy to leave behind.
        with _pooled(fake_client):
            response = client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "what is this?",
                    "stream": True,
                    "attachments": [{"mime_type": "image/png", "data": PNG_B64}],
                },
                headers=_auth(),
            )
        assert response.status_code == 200
        params = fake_client.send_prompt_streaming.call_args.kwargs["other_params"]
        assert params.included_data == [PNG]

    def test_a_prompt_with_no_attachments_still_sends_no_params(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        # Nothing to carry means nothing is built, so the plain path is
        # unchanged for every existing caller.
        with _pooled(fake_client):
            client.post(
                PATH, json={"engine": "claude", "prompt": "hi"}, headers=_auth()
            )
        assert fake_client.asend_prompt.await_args.kwargs["other_params"] is None


class TestTheLibraryOwnsThePolicy:
    """Which types are acceptable is the library's call, not a list here."""

    def test_a_pdf_is_refused_with_the_librarys_own_reason(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        # The library allows images only today. The service must surface that
        # as the caller's 400 rather than letting a ValueError become a 500.
        with _pooled(fake_client):
            response = client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "summarise",
                    "attachments": [{"mime_type": "application/pdf", "data": PNG_B64}],
                },
                headers=_auth(),
            )
        assert response.status_code == 400
        fake_client.asend_prompt.assert_not_awaited()

    def test_an_unclassifiable_mime_type_is_400(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        with _pooled(fake_client):
            response = client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "hi",
                    "attachments": [
                        {"mime_type": "application/x-widget", "data": PNG_B64}
                    ],
                },
                headers=_auth(),
            )
        assert response.status_code == 400
        assert "mime_type" in response.json()["detail"]

    def test_the_service_holds_no_allowlist_of_its_own(self) -> None:
        # If this ever grows into a copy of the library's policy, the two will
        # drift and the service will refuse things the provider accepts.
        from ai_api_unified_http.routes_v1 import _MIME_DATA_TYPES

        prefixes = {prefix for prefix, _ in _MIME_DATA_TYPES}
        assert "image/" in prefixes
        assert "audio/" in prefixes  # classified here, refused by the library
        assert "application/pdf" in prefixes


class TestMalformedAttachments:
    def test_bad_base64_is_400(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        with _pooled(fake_client):
            response = client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "hi",
                    "attachments": [{"mime_type": "image/png", "data": "not!base64!"}],
                },
                headers=_auth(),
            )
        assert response.status_code == 400
        assert "base64" in response.json()["detail"]

    def test_data_without_a_mime_type_is_400(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        with _pooled(fake_client):
            response = client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "hi",
                    "attachments": [{"data": PNG_B64}],
                },
                headers=_auth(),
            )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "attachment",
        [
            {},
            {"mime_type": "image/png", "data": PNG_B64, "artifact_id": "abcdefgh"},
        ],
        ids=["neither-source", "both-sources"],
    )
    def test_exactly_one_source_is_required(
        self, client: TestClient, fake_client: MagicMock, store, attachment: dict
    ) -> None:
        with _pooled(fake_client):
            response = client.post(
                PATH,
                json={"engine": "claude", "prompt": "hi", "attachments": [attachment]},
                headers=_auth(),
            )
        assert response.status_code == 400

    def test_too_many_attachments_is_422(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        response = client.post(
            PATH,
            json={
                "engine": "claude",
                "prompt": "hi",
                "attachments": [
                    {"mime_type": "image/png", "data": PNG_B64}
                    for _ in range(MAX_ATTACHMENTS + 1)
                ],
            },
            headers=_auth(),
        )
        assert response.status_code == 422


class TestArtifactAttachments:
    """An artifact id turns into a read, so it carries the same scoping."""

    def test_a_stored_artifact_can_be_attached_without_re_uploading(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        stored = artifacts.store_artifact("first", PNG, mime_type="image/png")
        with _pooled(fake_client):
            response = client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "describe the image you just made",
                    "attachments": [{"artifact_id": stored.artifact_id}],
                },
                headers=_auth(),
            )
        assert response.status_code == 200
        params = _params_of(fake_client.asend_prompt.await_args)
        assert params.included_data == [PNG]
        # The store knows what it wrote, so its type is used.
        assert params.included_mime_types == ["image/png"]

    def test_another_callers_artifact_is_not_readable_this_way(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        # Attaching must not become a way around the separation that fetching
        # the artifact directly already enforces.
        stored = artifacts.store_artifact("first", PNG, mime_type="image/png")
        with _pooled(fake_client):
            response = client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "what is this?",
                    "attachments": [{"artifact_id": stored.artifact_id}],
                },
                headers=_auth(SECOND_KEY),
            )
        assert response.status_code == 404
        fake_client.asend_prompt.assert_not_awaited()

    def test_an_unknown_artifact_is_404(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        with _pooled(fake_client):
            response = client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "hi",
                    "attachments": [{"artifact_id": artifacts.new_id()}],
                },
                headers=_auth(),
            )
        assert response.status_code == 404

    def test_a_traversing_artifact_id_cannot_read_a_file(
        self, client: TestClient, fake_client: MagicMock, store
    ) -> None:
        with _pooled(fake_client):
            response = client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "hi",
                    "attachments": [{"artifact_id": "../../etc/passwd"}],
                },
                headers=_auth(),
            )
        assert response.status_code == 404


class TestSpecSurface:
    def test_attachments_are_published_in_the_spec(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()
        props = spec["components"]["schemas"]["CompletionRequest"]["properties"]
        assert "attachments" in props
        assert "Attachment" in spec["components"]["schemas"]

    def test_the_attachment_cap_is_published(self, client: TestClient) -> None:
        # A caller generating a client should see the limit rather than
        # discovering it by being refused.
        spec = client.get("/openapi.json").json()
        field = spec["components"]["schemas"]["CompletionRequest"]["properties"][
            "attachments"
        ]
        assert "maxItems" in str(field)
