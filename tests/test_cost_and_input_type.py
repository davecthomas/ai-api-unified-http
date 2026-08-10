# tests/test_cost_and_input_type.py

"""
Two passthroughs the service was dropping on the floor.

`input_type` tells an embeddings provider whether it is reading a search query
or a stored document, and Voyage and Gemini embed the two differently. Losing
it produced vectors that looked fine and retrieved badly.

`usd_cost` prices a call the caller already has the usage for. The interesting
case is the model with no rates on record: the library's own helper answers
0.0 for it, which reads as "this call was free" and is the one answer that must
never appear.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ai_api_unified_http import rate_limit
from ai_api_unified_http.app import create_app
from ai_api_unified_http.auth import AUTH_DISABLED_ENV
from ai_api_unified_http.routes_v1 import _usd_cost

USAGE = SimpleNamespace(
    input_tokens=1_000,
    output_tokens=500,
    cached_input_tokens=0,
    total_tokens=1_500,
)


@pytest.fixture(autouse=True)
def clean_counter() -> None:
    rate_limit.reset_counter()
    yield
    rate_limit.reset_counter()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(AUTH_DISABLED_ENV, "1")
    return TestClient(create_app())


def _priced_client(
    input_rate: str = "3", output_rate: str = "15", cached_rate: str | None = None
) -> MagicMock:
    """A client whose model carries token rates, priced like a real one."""

    def compute_token_cost(
        *, input_tokens: int, output_tokens: int = 0, cached_input_tokens: int = 0
    ) -> Decimal:
        per_million = Decimal(1_000_000)
        cost = Decimal(input_tokens) * Decimal(input_rate) / per_million
        cost += Decimal(output_tokens) * Decimal(output_rate) / per_million
        rate = Decimal(cached_rate) if cached_rate is not None else Decimal(input_rate)
        cost += Decimal(cached_input_tokens) * rate / per_million
        return cost

    pricing = SimpleNamespace(
        token_rates=SimpleNamespace(input_per_1m=Decimal(input_rate)),
        compute_token_cost=compute_token_cost,
    )
    return MagicMock(capabilities=SimpleNamespace(pricing=pricing))


class TestCostComputation:
    def test_a_priced_model_reports_an_exact_decimal_string(self) -> None:
        # 1000 in at $3/1M plus 500 out at $15/1M = 0.003 + 0.0075.
        assert _usd_cost(_priced_client(), USAGE) == "0.0105"

    def test_the_figure_is_a_string_not_a_float(self) -> None:
        # Rates are strings for this reason; a cost that round-trips through
        # binary floating point would not equal what the provider bills.
        assert isinstance(_usd_cost(_priced_client(), USAGE), str)

    def test_a_model_with_no_pricing_reports_null(self) -> None:
        client = MagicMock(capabilities=SimpleNamespace(pricing=None))
        assert _usd_cost(client, USAGE) is None

    def test_pricing_without_token_rates_reports_null(self) -> None:
        # An image model prices per image, so it has pricing and no token
        # rates. Calling compute_token_cost on it raises.
        pricing = SimpleNamespace(token_rates=None)
        client = MagicMock(capabilities=SimpleNamespace(pricing=pricing))
        assert _usd_cost(client, USAGE) is None

    def test_null_is_distinguishable_from_a_genuinely_free_call(self) -> None:
        # The whole point: unpriced and free must not produce the same answer.
        free = SimpleNamespace(
            input_tokens=0, output_tokens=0, cached_input_tokens=0, total_tokens=0
        )
        assert _usd_cost(_priced_client(), free) == "0"
        assert (
            _usd_cost(MagicMock(capabilities=SimpleNamespace(pricing=None)), free)
            is None
        )

    def test_cached_tokens_are_billed_once_at_the_cached_rate(self) -> None:
        # compute_token_cost treats cached input as a subset of the input
        # count, so passing the full input alongside the cached count would
        # bill the cached tokens twice.
        usage = SimpleNamespace(
            input_tokens=1_000,
            output_tokens=0,
            cached_input_tokens=800,
            total_tokens=1_000,
        )
        # 200 uncached at $3/1M + 800 cached at $0.30/1M = 0.0006 + 0.00024.
        assert _usd_cost(_priced_client(cached_rate="0.30"), usage) == "0.00084"

    def test_a_client_with_no_capabilities_reports_null(self) -> None:
        assert _usd_cost(SimpleNamespace(), USAGE) is None

    def test_a_pricing_failure_reports_null_rather_than_raising(self) -> None:
        # The provider call has already happened and already been billed by the
        # time this runs. Raising would take away a result the caller paid for,
        # in exchange for a number they can compute from /v1/models.
        def explode(**_: object) -> Decimal:
            raise ValueError("rates are malformed")

        pricing = SimpleNamespace(
            token_rates=SimpleNamespace(input_per_1m=Decimal(3)),
            compute_token_cost=explode,
        )
        client = MagicMock(capabilities=SimpleNamespace(pricing=pricing))
        assert _usd_cost(client, USAGE) is None

    def test_a_completed_call_still_returns_when_pricing_fails(
        self, client: TestClient
    ) -> None:
        def explode(**_: object) -> Decimal:
            raise ArithmeticError("bad rate")

        pricing = SimpleNamespace(
            token_rates=SimpleNamespace(input_per_1m=Decimal(3)),
            compute_token_cost=explode,
        )
        turn = SimpleNamespace(
            text="hello",
            tool_calls=[],
            finish_reason=SimpleNamespace(value="complete"),
            usage=USAGE,
            raw_content=None,
        )
        fake = MagicMock(capabilities=SimpleNamespace(pricing=pricing))
        fake.asend_conversation = AsyncMock(return_value=turn)
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake
        ):
            response = client.post(
                "/v1/conversations/turn",
                json={
                    "engine": "claude",
                    "system_prompt": "be brief",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert response.status_code == 200
        assert response.json()["text"] == "hello"
        assert response.json()["usd_cost"] is None


class TestCostOnResponses:
    def test_structured_returns_the_cost(self, client: TestClient) -> None:
        result = SimpleNamespace(
            data={"ok": True},
            finish_reason=SimpleNamespace(value="complete"),
            usage=USAGE,
            raw_text='{"ok": true}',
        )
        fake = _priced_client()
        fake.asend_structured_output = AsyncMock(return_value=result)
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake
        ):
            response = client.post(
                "/v1/structured",
                json={
                    "engine": "claude",
                    "prompt": "hi",
                    "response_schema": {"type": "object"},
                },
            )
        assert response.status_code == 200
        assert response.json()["usd_cost"] == "0.0105"

    def test_a_conversation_turn_returns_the_cost(self, client: TestClient) -> None:
        turn = SimpleNamespace(
            text="hello",
            tool_calls=[],
            finish_reason=SimpleNamespace(value="complete"),
            usage=USAGE,
            raw_content=None,
        )
        fake = _priced_client()
        fake.asend_conversation = AsyncMock(return_value=turn)
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake
        ):
            response = client.post(
                "/v1/conversations/turn",
                json={
                    "engine": "claude",
                    "system_prompt": "be brief",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert response.status_code == 200
        assert response.json()["usd_cost"] == "0.0105"

    def test_an_unpriced_model_returns_null_not_zero(self, client: TestClient) -> None:
        turn = SimpleNamespace(
            text="hello",
            tool_calls=[],
            finish_reason=SimpleNamespace(value="complete"),
            usage=USAGE,
            raw_content=None,
        )
        fake = MagicMock(capabilities=SimpleNamespace(pricing=None))
        fake.asend_conversation = AsyncMock(return_value=turn)
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake
        ):
            response = client.post(
                "/v1/conversations/turn",
                json={
                    "engine": "claude",
                    "system_prompt": "be brief",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        body = response.json()
        assert body["usd_cost"] is None
        assert body["usd_cost"] != "0"


class TestEmbeddingsInputType:
    def _fake(self) -> MagicMock:
        fake = MagicMock()
        fake.agenerate_embeddings_batch = AsyncMock(
            return_value=[{"embedding": [0.1, 0.2]}]
        )
        return fake

    def test_input_type_reaches_the_provider(self, client: TestClient) -> None:
        fake = self._fake()
        with patch(
            "ai_api_unified_http.routes_v1.get_embeddings_client", return_value=fake
        ):
            client.post(
                "/v1/embeddings",
                json={"engine": "voyage", "inputs": ["a"], "input_type": "query"},
            )
        fake.agenerate_embeddings_batch.assert_awaited_once_with(
            ["a"], input_type="query"
        )

    def test_omitting_it_forwards_none(self, client: TestClient) -> None:
        fake = self._fake()
        with patch(
            "ai_api_unified_http.routes_v1.get_embeddings_client", return_value=fake
        ):
            client.post("/v1/embeddings", json={"engine": "voyage", "inputs": ["a"]})
        fake.agenerate_embeddings_batch.assert_awaited_once_with(["a"], input_type=None)

    def test_it_survives_the_sync_fallback(self, client: TestClient) -> None:
        # Gemini has no async embeddings, so the sync path runs in a
        # threadpool. The keyword has to survive that hop too.
        from ai_api_unified.ai_provider_exceptions import (
            AiProviderCapabilityUnsupportedError,
        )

        fake = MagicMock()
        fake.agenerate_embeddings_batch = AsyncMock(
            side_effect=AiProviderCapabilityUnsupportedError("no async")
        )
        fake.generate_embeddings_batch = MagicMock(
            return_value=[{"embedding": [0.3, 0.4]}]
        )
        with patch(
            "ai_api_unified_http.routes_v1.get_embeddings_client", return_value=fake
        ):
            response = client.post(
                "/v1/embeddings",
                json={
                    "engine": "google-gemini",
                    "inputs": ["a"],
                    "input_type": "document",
                },
            )
        assert response.status_code == 200
        fake.generate_embeddings_batch.assert_called_once_with(
            ["a"], input_type="document"
        )
