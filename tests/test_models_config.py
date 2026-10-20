# tests/test_models_config.py

"""
The shipped config/models.json, as the library resolves it.

Every selection the service relies on has to come from this file, because a
deployment sets no selection keys in its environment (`make gcp-deploy`
replaces the whole variable list). A capability the file leaves out has no
default provider in the library, so its endpoint would answer 503 on every
stock deployment while working for any developer whose `.env` still sets it.
"""

from pathlib import Path

import pytest
from ai_api_unified.ai_models_config import clear_models_config_cache
from ai_api_unified.util.env_settings import EnvSettings

REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# Every selection key the deprecated environment form had that the service
# needs. None of them may be set in the process for these tests: a value
# there would win over the file and hide a gap in it.
_SELECTION_KEYS: tuple[str, ...] = (
    "COMPLETIONS_ENGINE",
    "COMPLETIONS_MODEL_NAME",
    "COMPLETIONS_FALLBACKS",
    "COMPLETIONS_FALLBACK_ON",
    "EMBEDDING_ENGINE",
    "EMBEDDING_MODEL_NAME",
    "IMAGE_ENGINE",
    "IMAGE_MODEL_NAME",
    "VIDEO_ENGINE",
    "VIDEO_MODEL_NAME",
    "AI_VOICE_ENGINE",
    "GOOGLE_AUTH_METHOD",
)


@pytest.fixture(autouse=True)
def _file_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _SELECTION_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AI_MODELS_CONFIG", str(REPO_ROOT / "config" / "models.json"))
    clear_models_config_cache()
    yield
    clear_models_config_cache()


def _setting(key: str) -> str | None:
    value = EnvSettings().get_setting(key)
    return str(value) if value is not None else None


@pytest.mark.parametrize(
    "key",
    [
        "COMPLETIONS_ENGINE",
        "EMBEDDING_ENGINE",
        "IMAGE_ENGINE",
        "VIDEO_ENGINE",
        "AI_VOICE_ENGINE",
    ],
)
def test_every_capability_has_an_engine_in_the_file(key: str) -> None:
    # /v1/images takes no engine on the request, so without images.engine
    # here every stock deployment answered it with 503.
    assert _setting(key)


def test_production_defaults_have_no_fallback_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AI_MODELS_ENV", raising=False)
    assert _setting("COMPLETIONS_ENGINE") == "claude"
    assert _setting("COMPLETIONS_FALLBACKS") is None


def test_the_dev_overlay_carries_the_local_selections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AI_MODELS_ENV=dev in a developer's .env replaces the selection keys
    # that used to live there. Objects merge, so everything the overlay does
    # not name (media, voice, middleware) stays the shipped value.
    monkeypatch.setenv("AI_MODELS_ENV", "dev")
    clear_models_config_cache()
    assert _setting("COMPLETIONS_ENGINE") == "google-gemini"
    assert _setting("COMPLETIONS_FALLBACKS") == "openai:gpt-4o-mini"
    assert _setting("IMAGE_ENGINE") == "google-gemini"
    assert _setting("AI_VOICE_ENGINE") == "openai"


def test_the_dev_overlay_keeps_cost_capture_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # The startup gate refuses to serve without cost events. An overlay that
    # replaced the middleware list would trip it; this one must not.
    from ai_api_unified_http import cost

    monkeypatch.setenv("AI_MODELS_ENV", "dev")
    clear_models_config_cache()
    settings = cost._resolved_observability_settings()
    assert settings is not None and settings.emit_cost is True
