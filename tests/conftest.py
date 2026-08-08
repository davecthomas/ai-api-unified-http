# tests/conftest.py

"""
Shared test configuration.

Two service-wide startup gates exist — cost capture and authentication — and
both refuse to run unconfigured. Rather than repeat that setup in every module,
it lives here, so a new test file gets a service that starts and a client that
can reach the v1 surface.

Every fixture is function-scoped: the env vars are monkeypatched, and a
module-scoped client would outlive the patches that made it valid.
"""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_api_unified_http import auth, cost
from ai_api_unified_http.app import create_app

TEST_API_KEY: str = "test-suite-key"


@pytest.fixture(autouse=True)
def service_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Satisfy both startup gates, isolated per test.

    The cost topic is keyed on the test name because logging holds loggers in a
    process-wide registry, so a shared name would leak handlers between tests.
    """
    monkeypatch.setenv(auth.API_KEYS_ENV, f"tests:{TEST_API_KEY}")
    monkeypatch.delenv(auth.AUTH_DISABLED_ENV, raising=False)

    topic: str = f"test.{request.node.name}"
    monkeypatch.setenv(cost.COST_TOPIC_ENV, topic)
    monkeypatch.setenv(cost.COST_LOG_PATH_ENV, str(tmp_path / "cost-events.jsonl"))
    yield
    logging.getLogger(topic).handlers.clear()


@pytest.fixture
def client() -> TestClient:
    """An authenticated client for the v1 surface.

    The key is attached to every request, so tests that care about a specific
    endpoint do not restate authentication. Tests exercising auth itself build
    their own client and pass headers deliberately.
    """
    return TestClient(create_app(), headers={"Authorization": f"Bearer {TEST_API_KEY}"})


@pytest.fixture
def anonymous_client() -> TestClient:
    """A client that presents no credentials."""
    return TestClient(create_app())
