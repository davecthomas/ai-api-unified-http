# tests/test_voice.py

"""
The voice catalogue, and the guards in front of synthesis.

Engines differ more here than anywhere else in the API. One streams and has no
emotion control; another has emotion control and speech to text and does not
stream; one publishes nine voices and another publishes several thousand. A
caller cannot assume any of it, which is why the catalogue exists and why these
tests care that it reports the engine's own answers rather than a table.

The synthesis call itself is covered as far as a mocked client can take it —
that the right provider method is reached with the right arguments. Whether the
provider then produces audio is not something this suite can decide.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ai_api_unified_http import artifacts, clients, rate_limit
from ai_api_unified_http.app import create_app
from ai_api_unified_http.auth import API_KEYS_ENV
from ai_api_unified_http.schemas import MAX_VOICES_RETURNED

KEY: str = "voice-caller-key"
AUDIO: bytes = b"ID3\x04\x00" + b"audio" * 200


def _voice(voice_id: str, locale: str = "en-US") -> SimpleNamespace:
    return SimpleNamespace(
        voice_id=voice_id,
        voice_name=voice_id.title(),
        language="en",
        accent="american",
        locale=locale,
        gender="female",
    )


def _format(key: str, extension: str) -> SimpleNamespace:
    return SimpleNamespace(
        key=key, description=key, file_extension=extension, sample_rate_hz=24000
    )


def _capabilities(**overrides) -> SimpleNamespace:
    base = {
        "supports_ssml": False,
        "supports_streaming": True,
        "supports_speech_to_text": False,
        "supports_emotion_control": False,
        "supports_word_timestamps": False,
        "min_speaking_rate": 0.25,
        "max_speaking_rate": 4.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _client(voices=None, **capability_overrides) -> MagicMock:
    fake = MagicMock()
    fake.engine = "openai"
    fake.list_available_voices = voices or [_voice("alloy"), _voice("nova")]
    fake.list_output_formats = [
        _format("mp3_24000", ".mp3"),
        _format("flac_24000", ".flac"),
    ]
    fake.common_vendor_capabilities = _capabilities(**capability_overrides)
    fake.selected_voice = fake.list_available_voices[0]
    fake.default_audio_format = fake.list_output_formats[0]
    fake.text_to_voice = MagicMock(return_value=AUDIO)
    fake.text_to_voice_with_emotion_prompt = MagicMock(return_value=AUDIO)
    fake.get_audio_duration = MagicMock(return_value=1.5)
    fake.get_voices_by_locale = MagicMock(return_value=[_voice("alloy")])
    return fake


@pytest.fixture(autouse=True)
def clean() -> None:
    rate_limit.reset_counter()
    clients.reset_pools()
    yield
    rate_limit.reset_counter()
    clients.reset_pools()


@pytest.fixture
def store(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(artifacts.ARTIFACT_DIR_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, store) -> TestClient:
    monkeypatch.setenv(API_KEYS_ENV, f"voice:{KEY}")
    return TestClient(create_app())


def _auth() -> dict:
    return {"Authorization": f"Bearer {KEY}"}


def _pooled(fake: MagicMock):
    return patch("ai_api_unified_http.routes_v1.get_voice_client", return_value=fake)


class TestCatalogue:
    def test_it_reports_the_engines_own_answers(self, client: TestClient) -> None:
        fake = _client()
        with _pooled(fake):
            body = client.get("/v1/voices", headers=_auth()).json()

        assert body["engine"] == "openai"
        assert [v["voice_id"] for v in body["voices"]] == ["alloy", "nova"]
        assert [f["key"] for f in body["audio_formats"]] == ["mp3_24000", "flac_24000"]
        assert body["default_voice_id"] == "alloy"
        assert body["default_audio_format"] == "mp3_24000"

    def test_capabilities_come_from_the_engine_not_a_table(
        self, client: TestClient
    ) -> None:
        # The whole point of publishing these: two engines disagree, and a
        # caller reads the answer rather than guessing from the engine name.
        fake = _client(supports_emotion_control=True, supports_streaming=False)
        with _pooled(fake):
            caps = client.get("/v1/voices", headers=_auth()).json()["capabilities"]
        assert caps["supports_emotion_control"] is True
        assert caps["supports_streaming"] is False
        assert caps["max_speaking_rate"] == 4.0

    def test_a_large_catalogue_is_paged_and_the_total_is_honest(
        self, client: TestClient
    ) -> None:
        # One engine publishes thousands of voices. Returning them all would be
        # a payload rather than a menu, and truncating without saying so would
        # be a lie about what the engine offers.
        many = [_voice(f"v{n}") for n in range(MAX_VOICES_RETURNED + 250)]
        fake = _client(voices=many)
        with _pooled(fake):
            body = client.get("/v1/voices", headers=_auth()).json()

        assert len(body["voices"]) == MAX_VOICES_RETURNED
        assert body["total_voices"] == MAX_VOICES_RETURNED + 250

    def test_a_locale_narrows_the_list(self, client: TestClient) -> None:
        fake = _client()
        with _pooled(fake):
            body = client.get("/v1/voices?locale=en-GB", headers=_auth()).json()
        fake.get_voices_by_locale.assert_called_once_with("en-GB")
        assert body["total_voices"] == 1

    def test_the_catalogue_needs_a_key(self, client: TestClient) -> None:
        assert client.get("/v1/voices").status_code == 401


class TestSynthesisGuards:
    """Refusing here beats synthesizing something the engine cannot honour."""

    def test_emotion_is_refused_when_the_engine_reports_none(
        self, client: TestClient
    ) -> None:
        # Sending it anyway would be billed and the direction silently dropped.
        fake = _client(supports_emotion_control=False)
        with _pooled(fake):
            response = client.post(
                "/v1/speech",
                json={"text": "hello", "emotion_prompt": "sound cheerful"},
                headers=_auth(),
            )
        assert response.status_code == 400
        assert "emotion" in response.json()["detail"].lower()
        fake.text_to_voice_with_emotion_prompt.assert_not_called()

    def test_emotion_is_allowed_when_the_engine_reports_it(
        self, client: TestClient, store
    ) -> None:
        fake = _client(supports_emotion_control=True)
        with _pooled(fake):
            response = client.post(
                "/v1/speech",
                json={"text": "hello", "emotion_prompt": "sound cheerful"},
                headers=_auth(),
            )
        assert response.status_code == 200
        kwargs = fake.text_to_voice_with_emotion_prompt.call_args.kwargs
        assert kwargs["emotion_prompt"] == "sound cheerful"
        fake.text_to_voice.assert_not_called()

    def test_ssml_is_refused_when_the_engine_reports_none(
        self, client: TestClient
    ) -> None:
        fake = _client(supports_ssml=False)
        with _pooled(fake):
            response = client.post(
                "/v1/speech",
                json={"text": "<speak>hi</speak>", "use_ssml": True},
                headers=_auth(),
            )
        assert response.status_code == 400
        fake.text_to_voice.assert_not_called()

    def test_an_unknown_voice_is_refused_before_the_provider_is_called(
        self, client: TestClient
    ) -> None:
        fake = _client()
        with _pooled(fake):
            response = client.post(
                "/v1/speech", json={"text": "hi", "voice_id": "nope"}, headers=_auth()
            )
        assert response.status_code == 400
        assert "/v1/voices" in response.json()["detail"]
        fake.text_to_voice.assert_not_called()

    def test_an_unknown_format_is_refused(self, client: TestClient) -> None:
        fake = _client()
        with _pooled(fake):
            response = client.post(
                "/v1/speech",
                json={"text": "hi", "audio_format": "wav_96000"},
                headers=_auth(),
            )
        assert response.status_code == 400
        fake.text_to_voice.assert_not_called()

    def test_an_engine_with_no_defaults_is_503_not_a_type_error(
        self, client: TestClient
    ) -> None:
        # The base class defaults voice and format to None; every concrete
        # provider makes them required keyword arguments. Passing None through
        # would be a TypeError from inside the provider.
        fake = _client()
        fake.selected_voice = None
        fake.default_audio_format = None
        with _pooled(fake):
            response = client.post("/v1/speech", json={"text": "hi"}, headers=_auth())
        assert response.status_code == 503
        fake.text_to_voice.assert_not_called()

    def test_an_empty_result_is_502_not_an_empty_artifact(
        self, client: TestClient, store
    ) -> None:
        fake = _client()
        fake.text_to_voice = MagicMock(return_value=b"")
        with _pooled(fake):
            response = client.post("/v1/speech", json={"text": "hi"}, headers=_auth())
        assert response.status_code == 502

    def test_speech_needs_a_key(self, client: TestClient) -> None:
        assert client.post("/v1/speech", json={"text": "hi"}).status_code == 401


class TestSynthesisCallShape:
    """As far as a mocked client can go: the right call, the right arguments."""

    def test_the_provider_is_called_with_keywords_only(
        self, client: TestClient, store
    ) -> None:
        # The concrete providers narrow the base signature and accept nothing
        # positionally. This is the bug that a live call found and a mocked
        # test would otherwise have missed.
        fake = _client()
        with _pooled(fake):
            client.post(
                "/v1/speech",
                json={"text": "hello there", "speaking_rate": 1.25},
                headers=_auth(),
            )
        call = fake.text_to_voice.call_args
        assert call.args == ()
        assert call.kwargs["text_to_convert"] == "hello there"
        assert call.kwargs["speaking_rate"] == 1.25

    def test_defaults_are_resolved_to_real_objects(
        self, client: TestClient, store
    ) -> None:
        fake = _client()
        with _pooled(fake):
            client.post("/v1/speech", json={"text": "hi"}, headers=_auth())
        kwargs = fake.text_to_voice.call_args.kwargs
        assert kwargs["voice"] is fake.selected_voice
        assert kwargs["audio_format"] is fake.default_audio_format


class TestStoredClip:
    def test_the_clip_is_stored_and_returned_as_a_reference(
        self, client: TestClient, store
    ) -> None:
        fake = _client()
        with _pooled(fake):
            body = client.post(
                "/v1/speech",
                json={"text": "hi", "audio_format": "mp3_24000"},
                headers=_auth(),
            ).json()

        assert body["artifact"]["size_bytes"] == len(AUDIO)
        assert body["artifact"]["mime_type"] == "audio/mpeg"
        assert body["artifact"]["url_path"].startswith("/v1/artifacts/")
        assert body["duration_seconds"] == 1.5

    def test_the_clip_fetches_back_byte_for_byte(
        self, client: TestClient, store
    ) -> None:
        fake = _client()
        with _pooled(fake):
            body = client.post(
                "/v1/speech", json={"text": "hi"}, headers=_auth()
            ).json()
        fetched = client.get(body["artifact"]["url_path"], headers=_auth())
        assert fetched.content == AUDIO
        assert fetched.headers["content-length"] == str(len(AUDIO))

    def test_the_format_extension_decides_the_served_type(
        self, client: TestClient, store
    ) -> None:
        fake = _client()
        with _pooled(fake):
            body = client.post(
                "/v1/speech",
                json={"text": "hi", "audio_format": "flac_24000"},
                headers=_auth(),
            ).json()
        assert body["artifact"]["mime_type"] == "audio/flac"

    def test_a_missing_duration_does_not_fail_a_paid_call(
        self, client: TestClient, store
    ) -> None:
        # The clip exists and has been billed by then; its length is a
        # convenience, not the result.
        fake = _client()
        fake.get_audio_duration = MagicMock(side_effect=RuntimeError("no decoder"))
        with _pooled(fake):
            response = client.post("/v1/speech", json={"text": "hi"}, headers=_auth())
        assert response.status_code == 200
        assert response.json()["duration_seconds"] is None


class TestUnconfiguredEngine:
    def test_no_engine_configured_is_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AI_VOICE_ENGINE has no default, and the library says so. It reaches
        # the caller as a 503 naming the setting, not as a 500.
        monkeypatch.delenv("AI_VOICE_ENGINE", raising=False)
        response = client.get("/v1/voices", headers=_auth())
        assert response.status_code == 503
        assert "AI_VOICE_ENGINE" in response.json()["detail"]


class TestSurface:
    def test_both_paths_are_published(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert "/v1/voices" in paths
        assert "/v1/speech" in paths

    def test_speech_to_text_is_not_claimed(self, client: TestClient) -> None:
        # This PR is output only. The catalogue reports whether the engine can
        # transcribe, but no endpoint offers it yet.
        paths = client.get("/openapi.json").json()["paths"]
        assert not [p for p in paths if "transcri" in p or "stt" in p]
