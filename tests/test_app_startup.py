# tests/test_app_startup.py

"""
Startup refuses to serve when spend cannot be recorded.

TestClient runs the lifespan only when used as a context manager, so these
tests enter it deliberately. The rest of the suite constructs the client
directly and never triggers startup.
"""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_api_unified_http import cost
from ai_api_unified_http.app import create_app


@pytest.fixture(autouse=True)
def isolated_topic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    # Keyed on the test name, which is unique for the run. id() of a fixture
    # object is not: CPython reuses addresses after collection, so two tests
    # could silently share a topic and leak handlers between each other.
    topic = f"test.startup.{request.node.name}"
    monkeypatch.setenv(cost.COST_TOPIC_ENV, topic)
    monkeypatch.setenv(cost.COST_LOG_PATH_ENV, str(tmp_path / "cost-events.jsonl"))
    yield
    logging.getLogger(topic).handlers.clear()


def test_startup_attaches_cost_capture(tmp_path: Path) -> None:
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert logging.getLogger(cost.cost_topic()).handlers


def test_startup_fails_when_capture_cannot_be_established(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate the case verify_cost_capture exists to catch: the handler did
    # not land, so every cost event would be discarded. The service must not
    # come up and start spending money it cannot account for.
    #
    # Patched on the app module, not on cost: app.py binds the function at
    # import time, so replacing cost.attach_cost_handler would leave the real
    # one running and the test would pass for the wrong reason.
    monkeypatch.setattr("ai_api_unified_http.app.attach_cost_handler", lambda: None)

    with pytest.raises(cost.CostEventNotCapturedError), TestClient(create_app()):
        pass
