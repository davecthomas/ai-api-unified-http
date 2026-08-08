# tests/test_cost.py

"""
Cost capture is a hard requirement, so the tests cover the failure direction
as much as the happy one: a service that cannot record spend must refuse to
start rather than serve traffic that silently loses cost events.
"""

import json
import logging
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


def test_verify_fails_when_nothing_is_listening() -> None:
    with pytest.raises(cost.CostEventNotCapturedError) as caught:
        cost.verify_cost_capture()
    # The message must name the fix, not just the fault.
    assert cost.COST_LOG_PATH_ENV in str(caught.value)


def test_attach_then_verify_succeeds() -> None:
    cost.attach_cost_handler()
    cost.verify_cost_capture()


def test_events_land_as_json_lines(tmp_path: Path) -> None:
    cost.attach_cost_handler()
    logging.getLogger(cost.cost_topic()).info(
        "ai_api_call_cost", extra={"engine": "claude", "cost_usd": 0.0012}
    )

    written = (tmp_path / "cost-events.jsonl").read_text(encoding="utf-8").strip()
    event = json.loads(written)
    assert event["message"] == "ai_api_call_cost"
    assert event["engine"] == "claude"
    assert event["cost_usd"] == 0.0012


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
