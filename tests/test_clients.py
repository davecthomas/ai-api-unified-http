# tests/test_clients.py

"""
The client pool must build once per (engine, model) and reuse thereafter.

These tests patch AIFactory rather than constructing real clients: the pool's
whole reason to exist is that real construction re-reads .env, re-parses the
middleware YAML, and makes a network call on Gemini. The suite stays runnable
with no provider keys and no network.
"""

import threading
from unittest.mock import patch

import pytest

from ai_api_unified_http import clients


@pytest.fixture(autouse=True)
def clean_pools() -> None:
    """Every test starts and ends with empty pools."""
    clients.reset_pools()
    yield
    clients.reset_pools()


def test_same_key_builds_once_and_reuses() -> None:
    with patch(
        "ai_api_unified_http.clients.AIFactory.get_ai_completions_client"
    ) as factory:
        factory.side_effect = lambda **kwargs: object()

        first = clients.get_completions_client("claude", "claude-opus-5")
        second = clients.get_completions_client("claude", "claude-opus-5")

    assert first is second
    assert factory.call_count == 1


def test_distinct_models_are_distinct_pool_entries() -> None:
    with patch(
        "ai_api_unified_http.clients.AIFactory.get_ai_completions_client"
    ) as factory:
        factory.side_effect = lambda **kwargs: object()

        clients.get_completions_client("claude", "claude-opus-5")
        clients.get_completions_client("claude", "claude-sonnet-5")

    assert factory.call_count == 2
    assert clients.pool_sizes()["completions"] == 2


def test_default_model_is_a_separate_entry_from_a_named_one() -> None:
    # None means "engine default", which is not interchangeable with any
    # named model even when they happen to resolve to the same thing.
    with patch(
        "ai_api_unified_http.clients.AIFactory.get_ai_completions_client"
    ) as factory:
        factory.side_effect = lambda **kwargs: object()

        clients.get_completions_client("openai", None)
        clients.get_completions_client("openai", "gpt-5.4-mini")

    assert factory.call_count == 2


def test_completions_and_embeddings_pools_are_independent() -> None:
    with (
        patch(
            "ai_api_unified_http.clients.AIFactory.get_ai_completions_client"
        ) as completions,
        patch(
            "ai_api_unified_http.clients.AIFactory.get_ai_embedding_client"
        ) as embeddings,
    ):
        completions.side_effect = lambda **kwargs: object()
        embeddings.side_effect = lambda **kwargs: object()

        clients.get_completions_client("google-gemini", None)
        clients.get_embeddings_client("google-gemini", None)

    assert clients.pool_sizes() == {"completions": 1, "embeddings": 1}


def test_factory_receives_the_library_argument_names() -> None:
    # get_ai_completions_client and get_ai_embedding_client disagree on both
    # argument name and order, so a positional call would silently swap them.
    with (
        patch(
            "ai_api_unified_http.clients.AIFactory.get_ai_completions_client"
        ) as completions,
        patch(
            "ai_api_unified_http.clients.AIFactory.get_ai_embedding_client"
        ) as embeddings,
    ):
        clients.get_completions_client("claude", "claude-opus-5")
        clients.get_embeddings_client("voyage", "voyage-3")

    completions.assert_called_once_with(
        model_name="claude-opus-5", completions_engine="claude"
    )
    embeddings.assert_called_once_with(embedding_engine="voyage", model_name="voyage-3")


def test_concurrent_callers_converge_on_one_client() -> None:
    # Construction runs outside the lock, so a race may build more than once.
    # What must hold is that every caller leaves with the same instance.
    handed_out: list[object] = []
    barrier = threading.Barrier(8)

    def build(**kwargs: object) -> object:
        barrier.wait(timeout=5)
        return object()

    with patch(
        "ai_api_unified_http.clients.AIFactory.get_ai_completions_client"
    ) as factory:
        factory.side_effect = build

        def worker() -> None:
            handed_out.append(clients.get_completions_client("claude", "m"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

    assert len(handed_out) == 8
    assert len({id(client) for client in handed_out}) == 1
    assert clients.pool_sizes()["completions"] == 1


def test_construction_failure_does_not_poison_the_pool() -> None:
    # A cold key that fails must stay cold, so a later request can succeed
    # once the misconfiguration is fixed.
    with patch(
        "ai_api_unified_http.clients.AIFactory.get_ai_completions_client"
    ) as factory:
        factory.side_effect = RuntimeError("no credentials")
        with pytest.raises(RuntimeError):
            clients.get_completions_client("claude", "m")

    assert clients.pool_sizes()["completions"] == 0
