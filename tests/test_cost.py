# tests/test_cost.py

"""
Cost capture is a hard requirement, so the tests cover the failure direction
as much as the happy one: a service that cannot record spend must refuse to
start rather than serve traffic that silently loses cost events.
"""

import json
import logging
import os
from pathlib import Path

import pytest

from ai_api_unified_http import cost


@pytest.fixture(autouse=True)
def isolated_topic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Point each test at its own topic and sink.

    A unique topic per test matters: logging keeps loggers in a process-wide
    registry, so a shared name would leak handlers between tests. The key is
    the test name rather than id() of a fixture object, whose address CPython
    reuses after collection.
    """
    topic = f"test.cost.{request.node.name}"
    monkeypatch.setenv(cost.COST_TOPIC_ENV, topic)
    monkeypatch.setenv(cost.COST_LOG_PATH_ENV, str(tmp_path / "cost-events.jsonl"))
    yield
    logging.getLogger(topic).handlers.clear()


def _write_profile(tmp_path: Path, *, observability: bool, emit_cost: bool) -> str:
    """Write a middleware profile and return its path."""
    entries: list[str] = []
    if observability:
        entries.append(
            "  - name: observability\n"
            "    enabled: true\n"
            "    settings:\n"
            f"      emit_cost: {str(emit_cost).lower()}\n"
        )
    body = "middleware:\n" + ("".join(entries) if entries else "  []\n")
    path = tmp_path / "middleware.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestVerifyCostCapture:
    """Both halves must hold: the library must emit, and something must listen.

    Checking only the listener passes while nothing is recorded, because the
    library's emit_cost defaults to false and it then publishes to no one.
    """

    def test_fails_when_observability_middleware_is_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(
            cost.MIDDLEWARE_CONFIG_PATH_ENV,
            _write_profile(tmp_path, observability=False, emit_cost=False),
        )
        cost.attach_cost_handler()
        with pytest.raises(cost.CostEventNotCapturedError) as caught:
            cost.verify_cost_capture()
        assert "observability" in str(caught.value)

    def test_fails_when_emit_cost_is_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The library default. A handler is attached and listening, and still
        # nothing is ever recorded.
        monkeypatch.setenv(
            cost.MIDDLEWARE_CONFIG_PATH_ENV,
            _write_profile(tmp_path, observability=True, emit_cost=False),
        )
        cost.attach_cost_handler()
        with pytest.raises(cost.CostEventNotCapturedError) as caught:
            cost.verify_cost_capture()
        assert "emit_cost" in str(caught.value)

    def test_fails_when_nothing_is_listening(self) -> None:
        # Profile is fine (conftest points at the shipped one); no handler.
        with pytest.raises(cost.CostEventNotCapturedError) as caught:
            cost.verify_cost_capture()
        assert cost.COST_LOG_PATH_ENV in str(caught.value)

    def test_passes_when_the_library_emits_and_a_handler_listens(self) -> None:
        cost.attach_cost_handler()
        cost.verify_cost_capture()

    def test_shipped_profile_enables_cost_emission(self) -> None:
        # Guards the config file itself: if config/middleware.yaml ever loses
        # emit_cost, this fails rather than the service silently under-recording.
        settings = cost._resolved_observability_settings()
        assert settings is not None
        assert settings.emit_cost is True


class TestMiddlewareDefault:
    def test_default_is_applied_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(cost.MIDDLEWARE_CONFIG_PATH_ENV, raising=False)
        monkeypatch.chdir(Path(__file__).resolve().parent.parent)
        cost.apply_default_middleware_config()
        assert (
            cost.DEFAULT_MIDDLEWARE_CONFIG_PATH
            in os.environ[cost.MIDDLEWARE_CONFIG_PATH_ENV]
        )

    def test_an_operator_supplied_profile_is_not_overridden(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        chosen = _write_profile(tmp_path, observability=True, emit_cost=True)
        monkeypatch.setenv(cost.MIDDLEWARE_CONFIG_PATH_ENV, chosen)
        cost.apply_default_middleware_config()
        assert os.environ[cost.MIDDLEWARE_CONFIG_PATH_ENV] == chosen


def test_events_land_as_json_lines(tmp_path: Path) -> None:
    cost.attach_cost_handler()
    logging.getLogger(cost.cost_topic()).info(
        "ai_api_call_cost", extra={"engine": "claude", "cost_usd": 0.0012}
    )

    written = (tmp_path / "cost-events.jsonl").read_text(encoding="utf-8").strip()
    event = json.loads(written)
    assert event["event"] == "ai_api_call_cost"
    assert event["engine"] == "claude"
    assert event["cost_usd"] == 0.0012


def test_the_librarys_own_event_shape_is_flattened(tmp_path: Path) -> None:
    # The library logs "%s %s" with (event_name, payload), so the structured
    # fields live in args rather than on the record.
    cost.attach_cost_handler()
    logging.getLogger(cost.cost_topic()).info(
        "%s %s",
        "ai_api_call_cost",
        {"model": "claude-haiku-4-5", "usd_cost": "0.000035", "input_tokens": 15},
    )

    event = json.loads((tmp_path / "cost-events.jsonl").read_text(encoding="utf-8"))
    assert event["event"] == "ai_api_call_cost"
    assert event["model"] == "claude-haiku-4-5"
    assert event["usd_cost"] == "0.000035"
    assert event["input_tokens"] == 15


def test_logging_internals_are_not_written_to_the_sink(tmp_path: Path) -> None:
    # Filtering against LogRecord's class __dict__ catches only methods, which
    # let pathname, lineno, thread and the rest into every event.
    cost.attach_cost_handler()
    logging.getLogger(cost.cost_topic()).info(
        "%s %s", "ai_api_call_cost", {"model": "m"}
    )

    event = json.loads((tmp_path / "cost-events.jsonl").read_text(encoding="utf-8"))
    for noise in ("pathname", "lineno", "thread", "processName", "msg", "args"):
        assert noise not in event


def test_library_fields_pass_through_without_being_named(tmp_path: Path) -> None:
    # The handler forwards every non-standard record attribute rather than
    # listing known fields, so a new library field survives with no change here.
    cost.attach_cost_handler()
    logging.getLogger(cost.cost_topic()).info(
        "ai_api_call_cost", extra={"a_field_added_later": "carried"}
    )

    event = json.loads((tmp_path / "cost-events.jsonl").read_text(encoding="utf-8"))
    assert event["a_field_added_later"] == "carried"


def test_attach_is_idempotent() -> None:
    first = cost.attach_cost_handler()
    second = cost.attach_cost_handler()

    assert first is second
    assert len(logging.getLogger(cost.cost_topic()).handlers) == 1


def test_repeat_attachment_does_not_double_events(tmp_path: Path) -> None:
    cost.attach_cost_handler()
    cost.attach_cost_handler()
    logging.getLogger(cost.cost_topic()).info("ai_api_call_cost")

    lines = (tmp_path / "cost-events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_topic_records_at_info_regardless_of_root_level(tmp_path: Path) -> None:
    # A deployment running the root logger at WARNING must not thereby drop
    # every cost event.
    logging.getLogger().setLevel(logging.WARNING)
    cost.attach_cost_handler()
    logging.getLogger(cost.cost_topic()).info("ai_api_call_cost")

    assert (tmp_path / "cost-events.jsonl").read_text(encoding="utf-8").strip()


def test_sink_directory_is_created(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    nested = tmp_path / "does" / "not" / "exist" / "cost.jsonl"
    monkeypatch.setenv(cost.COST_LOG_PATH_ENV, str(nested))
    cost.attach_cost_handler()
    logging.getLogger(cost.cost_topic()).info("ai_api_call_cost")

    assert nested.exists()


def test_a_failing_sink_does_not_raise_into_the_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Losing an event is bad; taking down the request that produced it is
    # worse. The handler must swallow its own write failures.
    cost.attach_cost_handler()
    handler = logging.getLogger(cost.cost_topic()).handlers[0]
    monkeypatch.setattr(handler, "path", tmp_path)  # a directory, not a file
    logging.raiseExceptions = False
    try:
        logging.getLogger(cost.cost_topic()).info("ai_api_call_cost")
    finally:
        logging.raiseExceptions = True
