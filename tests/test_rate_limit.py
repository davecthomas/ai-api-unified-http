# tests/test_rate_limit.py

"""
The limiter guards spend, not abuse.

Authentication answers who may call; this answers how much they may spend,
since every /v1 call bills the deployment's provider account. The tests care
about the boundaries: the last allowed request, the first refused one, whether
one caller's budget can be consumed by another, and whether the public paths a
load balancer needs stay reachable when a key is exhausted.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ai_api_unified_http import rate_limit
from ai_api_unified_http.app import create_app
from ai_api_unified_http.auth import API_KEYS_ENV

FIRST_KEY: str = "first-caller-key"
SECOND_KEY: str = "second-caller-key"
PATH: str = "/v1/tokens/count"
BODY: dict = {"engine": "claude", "prompt": "hi"}


@pytest.fixture(autouse=True)
def clean_counter() -> None:
    rate_limit.reset_counter()
    yield
    rate_limit.reset_counter()


@pytest.fixture
def pooled():
    """Keep requests inside the process; the limiter is what is under test."""
    fake = MagicMock()
    fake.count_tokens = MagicMock(return_value=3)
    with patch(
        "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake
    ):
        yield fake


@pytest.fixture
def two_key_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(API_KEYS_ENV, f"first:{FIRST_KEY}, second:{SECOND_KEY}")
    return TestClient(create_app())


def _post(client: TestClient, key: str):
    return client.post(PATH, json=BODY, headers={"Authorization": f"Bearer {key}"})


class TestLimit:
    def test_requests_under_the_limit_are_served(
        self, monkeypatch: pytest.MonkeyPatch, two_key_client: TestClient, pooled
    ) -> None:
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "3")
        for _ in range(3):
            assert _post(two_key_client, FIRST_KEY).status_code == 200

    def test_the_request_past_the_limit_is_429(
        self, monkeypatch: pytest.MonkeyPatch, two_key_client: TestClient, pooled
    ) -> None:
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "3")
        for _ in range(3):
            _post(two_key_client, FIRST_KEY)

        response = _post(two_key_client, FIRST_KEY)
        assert response.status_code == 429
        assert response.json()["error"] == "rate_limited"

    def test_a_429_tells_the_caller_when_to_retry(
        self, monkeypatch: pytest.MonkeyPatch, two_key_client: TestClient, pooled
    ) -> None:
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "1")
        _post(two_key_client, FIRST_KEY)
        response = _post(two_key_client, FIRST_KEY)

        assert int(response.headers["Retry-After"]) >= 1
        assert response.headers["X-RateLimit-Remaining"] == "0"

    def test_remaining_is_reported_on_success(
        self, monkeypatch: pytest.MonkeyPatch, two_key_client: TestClient, pooled
    ) -> None:
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "5")
        response = _post(two_key_client, FIRST_KEY)
        assert response.headers["X-RateLimit-Remaining"] == "4"
        assert response.headers["X-RateLimit-Limit"] == "5"

    def test_one_callers_usage_does_not_consume_anothers(
        self, monkeypatch: pytest.MonkeyPatch, two_key_client: TestClient, pooled
    ) -> None:
        # Counting per key is the whole point: a noisy caller must not be able
        # to lock out everyone else sharing the deployment.
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "2")
        for _ in range(3):
            _post(two_key_client, FIRST_KEY)

        assert _post(two_key_client, SECOND_KEY).status_code == 200

    def test_zero_disables_the_limiter(
        self, monkeypatch: pytest.MonkeyPatch, two_key_client: TestClient, pooled
    ) -> None:
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "0")
        for _ in range(25):
            assert _post(two_key_client, FIRST_KEY).status_code == 200

    def test_an_unparseable_limit_falls_back_rather_than_disabling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A typo must not silently remove the spend guard.
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "sixty")
        assert rate_limit.rate_limit() == rate_limit.DEFAULT_RATE_LIMIT

    def test_a_negative_limit_is_treated_as_disabled_not_as_a_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "-5")
        assert rate_limit.rate_limit() == 0


class TestExemptions:
    def test_healthz_is_never_limited(
        self, monkeypatch: pytest.MonkeyPatch, two_key_client: TestClient, pooled
    ) -> None:
        # A load balancer polling health must not be locked out by a caller
        # exhausting their budget, and health costs nothing to serve.
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "1")
        _post(two_key_client, FIRST_KEY)
        _post(two_key_client, FIRST_KEY)

        for _ in range(10):
            assert two_key_client.get("/healthz").status_code == 200

    def test_the_openapi_document_is_never_limited(
        self, monkeypatch: pytest.MonkeyPatch, two_key_client: TestClient
    ) -> None:
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "1")
        for _ in range(5):
            assert two_key_client.get("/openapi.json").status_code == 200

    def test_preflight_is_not_counted(
        self, monkeypatch: pytest.MonkeyPatch, two_key_client: TestClient
    ) -> None:
        # A browser sends one preflight per request; counting them would halve
        # every browser caller's real budget.
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "2")
        for _ in range(6):
            response = two_key_client.options(
                PATH,
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert response.status_code == 200


class TestWindow:
    def test_window_resets_the_budget(
        self, monkeypatch: pytest.MonkeyPatch, two_key_client: TestClient, pooled
    ) -> None:
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "1")
        monkeypatch.setenv(rate_limit.RATE_WINDOW_ENV, "1")

        assert _post(two_key_client, FIRST_KEY).status_code == 200
        assert _post(two_key_client, FIRST_KEY).status_code == 429

        # Advance past the window rather than sleeping through it.
        rate_limit._counter._window_started -= 2
        assert _post(two_key_client, FIRST_KEY).status_code == 200

    def test_a_window_reset_clears_every_caller(self) -> None:
        # The reset drops the whole dict, which is also what keeps it from
        # growing without bound as keys rotate.
        counter = rate_limit.FixedWindowCounter()
        counter.hit("a", limit=5, window=1)
        counter.hit("b", limit=5, window=1)
        counter._window_started -= 2
        allowed, remaining, _ = counter.hit("a", limit=5, window=1)

        assert allowed
        assert remaining == 4


def test_counting_is_safe_across_threads() -> None:
    # Streaming requests are served from a threadpool, so several threads
    # reach the counter at once. An unguarded count would over-admit.
    import threading

    counter = rate_limit.FixedWindowCounter()
    outcomes: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(12)

    def worker() -> None:
        barrier.wait(timeout=5)
        allowed, _, _ = counter.hit("shared", limit=5, window=60)
        with lock:
            outcomes.append(allowed)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(outcomes) == 12
    assert sum(outcomes) == 5
