# tests/test_logging_setup.py

"""
Without configured logging the service's own decisions go nowhere: which
middleware profile is in use, where cost events land, whether auth is off.
These tests cover the level resolution and the two exemptions.
"""

import logging

import pytest

from ai_api_unified_http import logging_setup


@pytest.fixture(autouse=True)
def restore_root_logger() -> None:
    """Put the root logger back after each test."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers[:] = original_handlers
    root.setLevel(original_level)


def test_default_level_is_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(logging_setup.LOG_LEVEL_ENV, raising=False)
    assert logging_setup.configure_logging() == logging.INFO


@pytest.mark.parametrize(
    "value,expected",
    [
        ("DEBUG", logging.DEBUG),
        ("warning", logging.WARNING),
        ("  error  ", logging.ERROR),
        ("CRITICAL", logging.CRITICAL),
    ],
)
def test_level_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: int
) -> None:
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, value)
    assert logging_setup.configure_logging() == expected


@pytest.mark.parametrize("value", ["", "NOT_A_LEVEL", "17x"])
def test_an_unparseable_level_falls_back_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # A typo in one variable should not stop the service from starting.
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, value)
    assert logging_setup.configure_logging() == logging.INFO


def test_configuration_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reload-driven restarts must not stack handlers and double every line.
    monkeypatch.delenv(logging_setup.LOG_LEVEL_ENV, raising=False)
    logging_setup.configure_logging()
    count_after_first = len(logging.getLogger().handlers)
    logging_setup.configure_logging()

    assert len(logging.getLogger().handlers) == count_after_first


def test_an_existing_root_handler_is_adopted_rather_than_doubled() -> None:
    # uvicorn installs a root handler before the app starts. Adding a second
    # prints every line twice, which is what shipped before this check.
    root = logging.getLogger()
    root.handlers[:] = [logging.StreamHandler()]

    logging_setup.configure_logging()

    assert len(root.handlers) == 1


def test_a_handler_is_installed_when_the_root_has_none() -> None:
    # Running outside uvicorn, nothing else configures logging, so the service
    # must install its own or its messages go nowhere.
    root = logging.getLogger()
    root.handlers[:] = []

    logging_setup.configure_logging()

    assert root.handlers


def test_noisy_library_loggers_are_quieted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "INFO")
    logging_setup.configure_logging()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("anthropic").level == logging.WARNING


def test_debug_keeps_library_loggers_verbose(monkeypatch: pytest.MonkeyPatch) -> None:
    # Asking for DEBUG is asking for the provider chatter too.
    for name in logging_setup._NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "DEBUG")
    logging_setup.configure_logging()

    assert logging.getLogger("httpx").level != logging.WARNING


def test_service_messages_are_not_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    # The failure this guards: with no configuration the root logger discards
    # INFO, so every decision the service logs goes nowhere.
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "INFO")
    logging_setup.configure_logging()

    service_logger = logging.getLogger("ai_api_unified_http.somewhere")
    assert service_logger.isEnabledFor(logging.INFO)
    assert logging.getLogger().handlers


def test_a_raised_level_does_discard_service_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "ERROR")
    logging_setup.configure_logging()

    assert not logging.getLogger("ai_api_unified_http.somewhere").isEnabledFor(
        logging.INFO
    )
