# tests/test_config.py

"""
Nothing else reads the env file.

The library resolves the service's own settings from `os.environ`, so a `.env`
on disk is inert until something puts them there.
"""

import os
from pathlib import Path

import pytest

from ai_api_unified_http import config


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # This module exercises the default relative path, so it opts out of the
    # suite-wide override that keeps every other test off the developer's .env.
    monkeypatch.delenv(config.ENV_FILE_ENV, raising=False)
    monkeypatch.delenv("A_TEST_PROVIDER_KEY", raising=False)
    monkeypatch.delenv("ANOTHER_TEST_VALUE", raising=False)
    monkeypatch.chdir(tmp_path)
    yield


def _write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_values_are_loaded_into_the_environment(tmp_path: Path) -> None:
    _write_env(tmp_path, "A_TEST_PROVIDER_KEY=from-file\nANOTHER_TEST_VALUE=2\n")

    assert config.load_env_file() == 2
    assert os.environ["A_TEST_PROVIDER_KEY"] == "from-file"


def test_a_real_environment_variable_is_never_overridden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A deployment injects configuration through its own environment. A stale
    # .env left in an image must not quietly replace it.
    monkeypatch.setenv("A_TEST_PROVIDER_KEY", "from-deployment")
    _write_env(tmp_path, "A_TEST_PROVIDER_KEY=from-file\n")

    config.load_env_file()

    assert os.environ["A_TEST_PROVIDER_KEY"] == "from-deployment"


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    # Normal for a deployment that injects its own environment.
    assert config.load_env_file() == 0


def test_the_path_is_overridable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "config" / "service.env"
    elsewhere.parent.mkdir()
    elsewhere.write_text("A_TEST_PROVIDER_KEY=elsewhere\n", encoding="utf-8")
    monkeypatch.setenv(config.ENV_FILE_ENV, str(elsewhere))

    assert config.load_env_file() == 1
    assert os.environ["A_TEST_PROVIDER_KEY"] == "elsewhere"


def test_comments_and_blank_lines_are_ignored(tmp_path: Path) -> None:
    _write_env(
        tmp_path,
        "# a comment\n\nA_TEST_PROVIDER_KEY=value\n\n# another\n",
    )

    assert config.load_env_file() == 1
    assert os.environ["A_TEST_PROVIDER_KEY"] == "value"


def test_an_empty_assignment_is_applied_as_empty(tmp_path: Path) -> None:
    # An empty assignment is loaded as the empty string, not skipped. This
    # matters for COMPLETIONS_MODEL_NAME, where an empty value is forwarded to
    # the provider and rejected — commenting the line out is what selects the
    # engine default.
    _write_env(tmp_path, "A_TEST_PROVIDER_KEY=\n")

    config.load_env_file()

    assert os.environ["A_TEST_PROVIDER_KEY"] == ""


def test_service_owned_variables_reach_os_environ(tmp_path: Path) -> None:
    """Service-owned variables must reach `os.environ`, not just a settings model.

    The library reads `.env` through pydantic-settings, which populates its
    own model. The service reads its variables from `os.environ` directly, so
    a `.env` holding HTTP_API_KEYS leaves the service with no keys configured
    unless the file is loaded here.
    """
    from ai_api_unified_http.auth import API_KEYS_ENV, load_api_keys

    os.environ.pop(API_KEYS_ENV, None)
    _write_env(tmp_path, f"{API_KEYS_ENV}=webapp:secret-from-file\n")

    assert load_api_keys() == {}
    config.load_env_file()
    assert load_api_keys() == {"secret-from-file": "webapp"}
