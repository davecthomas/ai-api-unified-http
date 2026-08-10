# tests/test_catalog_and_utility.py

"""
The three read/utility endpoints.

`/v1/embeddings` and `/v1/models` both have to survive provider disagreement:
providers use different keys for an embedding vector, and the registry does not
carry an entry for every model a provider reports. Both cases are covered here
because both are silent-wrong-answer risks rather than crashes.
"""

import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ai_api_unified import (
    AiProviderCapabilityUnsupportedError,
    ModelLifecycleStatus,
)
from fastapi.testclient import TestClient


class TestEmbeddings:
    @pytest.fixture
    def pooled(self):
        fake = MagicMock()
        fake.agenerate_embeddings_batch = AsyncMock(
            return_value=[
                {"embedding": [0.1, 0.2, 0.3]},
                {"embedding": [0.4, 0.5, 0.6]},
            ]
        )
        with patch(
            "ai_api_unified_http.routes_v1.get_embeddings_client", return_value=fake
        ) as getter:
            yield getter, fake

    def test_returns_one_vector_per_input(
        self, client: TestClient, pooled: tuple
    ) -> None:
        response = client.post(
            "/v1/embeddings",
            json={"engine": "google-gemini", "inputs": ["a", "b"]},
        )
        assert response.status_code == 200
        body = response.json()
        assert [v["index"] for v in body["vectors"]] == [0, 1]
        assert body["vectors"][0]["embedding"] == [0.1, 0.2, 0.3]
        assert body["dimensions"] == 3

    def test_uses_the_batch_call_once(self, client: TestClient, pooled: tuple) -> None:
        # N single calls would multiply provider round trips and cost events
        # for one request.
        _, fake = pooled
        client.post("/v1/embeddings", json={"engine": "openai", "inputs": ["a", "b"]})
        fake.agenerate_embeddings_batch.assert_awaited_once_with(
            ["a", "b"], input_type=None
        )

    def test_empty_inputs_is_400_before_any_provider_call(
        self, client: TestClient, pooled: tuple
    ) -> None:
        getter, _ = pooled
        response = client.post(
            "/v1/embeddings", json={"engine": "openai", "inputs": []}
        )
        assert response.status_code == 400
        getter.assert_not_called()

    @pytest.mark.parametrize("key", ["embedding", "values", "vector"])
    def test_provider_vector_key_variants_are_read(
        self, client: TestClient, key: str
    ) -> None:
        # The library hands back the provider's own dict, and providers
        # disagree on the key.
        fake = MagicMock()
        fake.agenerate_embeddings_batch = AsyncMock(return_value=[{key: [1.0, 2.0]}])
        with patch(
            "ai_api_unified_http.routes_v1.get_embeddings_client", return_value=fake
        ):
            response = client.post(
                "/v1/embeddings", json={"engine": "voyage", "inputs": ["a"]}
            )
        assert response.status_code == 200
        assert response.json()["vectors"][0]["embedding"] == [1.0, 2.0]

    def test_falls_back_to_the_sync_call_when_async_is_unsupported(
        self, client: TestClient
    ) -> None:
        # Gemini's embedding models raise for the async surface and Bedrock has
        # no async at all, so the sync call runs in the threadpool instead of
        # the request failing.
        fake = MagicMock()
        fake.agenerate_embeddings_batch = AsyncMock(
            side_effect=AiProviderCapabilityUnsupportedError("no async embeddings")
        )
        fake.generate_embeddings_batch = MagicMock(
            return_value=[{"embedding": [9.0, 8.0]}]
        )
        with patch(
            "ai_api_unified_http.routes_v1.get_embeddings_client", return_value=fake
        ):
            response = client.post(
                "/v1/embeddings", json={"engine": "google-gemini", "inputs": ["a"]}
            )

        assert response.status_code == 200
        assert response.json()["vectors"][0]["embedding"] == [9.0, 8.0]
        fake.generate_embeddings_batch.assert_called_once_with(["a"], input_type=None)

    def test_other_capability_errors_still_surface(self, client: TestClient) -> None:
        # The fallback covers "no async", not every capability failure: a sync
        # call that also fails must reach the caller rather than being hidden.
        fake = MagicMock()
        fake.agenerate_embeddings_batch = AsyncMock(
            side_effect=AiProviderCapabilityUnsupportedError("no async")
        )
        fake.generate_embeddings_batch = MagicMock(
            side_effect=AiProviderCapabilityUnsupportedError("model cannot embed")
        )
        with patch(
            "ai_api_unified_http.routes_v1.get_embeddings_client", return_value=fake
        ):
            response = client.post(
                "/v1/embeddings", json={"engine": "google-gemini", "inputs": ["a"]}
            )
        assert response.status_code == 400

    def test_an_unrecognized_result_shape_fails_loudly(
        self, client: TestClient
    ) -> None:
        # A silently empty vector would be worse than a clear failure: the
        # caller would store zeros and never know.
        fake = MagicMock()
        fake.agenerate_embeddings_batch = AsyncMock(return_value=[{"surprise": [1.0]}])
        with patch(
            "ai_api_unified_http.routes_v1.get_embeddings_client", return_value=fake
        ):
            response = client.post(
                "/v1/embeddings", json={"engine": "openai", "inputs": ["a"]}
            )
        assert response.status_code == 502
        assert "surprise" in response.json()["detail"]


class TestTokenCount:
    def test_returns_the_provider_count(self, client: TestClient) -> None:
        fake = MagicMock()
        fake.count_tokens = MagicMock(return_value=42)
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake
        ):
            response = client.post(
                "/v1/tokens/count", json={"engine": "claude", "prompt": "hello"}
            )
        assert response.status_code == 200
        assert response.json()["token_count"] == 42
        fake.count_tokens.assert_called_once_with("hello")


class TestModels:
    @pytest.fixture
    def registry(self):
        info = MagicMock()
        info.provider = "anthropic"
        info.model = "claude-haiku-4-5"
        info.status = ModelLifecycleStatus.DEPRECATED
        info.sunset_date = datetime.date(2027, 1, 1)
        info.recommended_replacement = "claude-haiku-5"
        rates = MagicMock()
        rates.input_per_1m = Decimal("0.075")
        rates.output_per_1m = Decimal("0.30")
        rates.cached_input_per_1m = None
        pricing = MagicMock()
        pricing.unit.value = "per_1m_tokens"
        pricing.currency = "USD"
        pricing.effective_date = datetime.date(2026, 7, 7)
        pricing.source = "https://example.test/pricing"
        pricing.confidence = "high"
        pricing.token_rates = rates
        pricing.notes = None
        info.pricing = pricing
        return info

    def test_lists_models_with_catalog_entries(
        self, client: TestClient, registry: MagicMock
    ) -> None:
        with (
            patch(
                "ai_api_unified_http.routes_v1.get_completions_client",
                return_value=MagicMock(),
            ),
            patch(
                "ai_api_unified_http.routes_v1.AIFactory.list_completion_models",
                return_value=["claude-haiku-4-5"],
            ),
            patch(
                "ai_api_unified_http.routes_v1.get_model_info", return_value=registry
            ),
        ):
            response = client.get("/v1/models", params={"engine": "claude"})

        assert response.status_code == 200
        body = response.json()
        assert body["models"] == ["claude-haiku-4-5"]
        entry = body["catalog"][0]
        assert entry["status"] == "deprecated"
        assert entry["recommended_replacement"] == "claude-haiku-5"
        assert entry["sunset_date"] == "2027-01-01"

    def test_money_is_serialized_as_strings(
        self, client: TestClient, registry: MagicMock
    ) -> None:
        # Decimal money cannot round-trip through binary floating point. A
        # rate of 0.075 arriving as 0.07499999999999999 would be wrong in a
        # field callers may use to compute cost.
        with (
            patch(
                "ai_api_unified_http.routes_v1.get_completions_client",
                return_value=MagicMock(),
            ),
            patch(
                "ai_api_unified_http.routes_v1.AIFactory.list_completion_models",
                return_value=["claude-haiku-4-5"],
            ),
            patch(
                "ai_api_unified_http.routes_v1.get_model_info", return_value=registry
            ),
        ):
            body = client.get("/v1/models", params={"engine": "claude"}).json()

        rates = body["catalog"][0]["pricing"]["token_rates"]
        assert rates["input_per_1m"] == "0.075"
        assert rates["output_per_1m"] == "0.30"
        assert rates["cached_input_per_1m"] is None

    def test_models_without_a_registry_entry_are_still_listed(
        self, client: TestClient
    ) -> None:
        # A provider may report models the registry has not catalogued. They
        # belong in `models` even with no `catalog` entry, so the two lists are
        # reported separately rather than merged.
        with (
            patch(
                "ai_api_unified_http.routes_v1.get_completions_client",
                return_value=MagicMock(),
            ),
            patch(
                "ai_api_unified_http.routes_v1.AIFactory.list_completion_models",
                return_value=["brand-new-model"],
            ),
            patch("ai_api_unified_http.routes_v1.get_model_info", return_value=None),
        ):
            body = client.get("/v1/models", params={"engine": "openai"}).json()

        assert body["models"] == ["brand-new-model"]
        assert body["catalog"] == []

    def test_engine_is_required(self, client: TestClient) -> None:
        assert client.get("/v1/models").status_code == 422

    def test_registry_lookup_bridges_engine_to_provider(
        self, client: TestClient
    ) -> None:
        # The registry keys on the provider vendor ("anthropic") while callers
        # select an engine ("claude"), so a lookup by engine matches nothing
        # and yields an empty catalog. The lookup searches the registry's own
        # keys instead. Runs against the real registry, not a mock.
        from ai_api_unified_http.routes_v1 import _registry_entry

        entry = _registry_entry("claude-haiku-4-5")
        assert entry is not None
        assert entry.provider == "anthropic"

    def test_an_uncatalogued_model_returns_none(self) -> None:
        from ai_api_unified_http.routes_v1 import _registry_entry

        assert _registry_entry("model-that-does-not-exist") is None
