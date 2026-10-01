# tests/test_fallback.py

"""
Model fallback across the service.

The chain itself is the library's (`AiFallbackCompletions`) and is tested
there. What the service owns, and what these tests pin, is everything around
it: which pooled clients carry the chain, telling the caller which model
answered, pricing at that model, a startup check on the settings, and the
reason a failure carries when every model failed.

The routes run against the real wrapper around fake engines, so the log event
and the result stamps the service reads are the ones the library produces.
"""

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from ai_api_unified import (
    AIFallbackCandidate,
    AiFallbackCompletions,
    AiFallbackReason,
    AIFinishReason,
    AiProviderRequestError,
    AIStructuredOutputResult,
    AITokenUsage,
    AITurnResult,
)
from ai_api_unified.ai_base import AIBaseCompletions, AICompletionsCapabilitiesBase
from fastapi.testclient import TestClient

from ai_api_unified_http import clients
from ai_api_unified_http.fallback import (
    COMPLETIONS_FALLBACK_ON_ENV,
    COMPLETIONS_FALLBACKS_ENV,
    FallbackMisconfiguredError,
    verify_fallback_config,
)

PATH: str = "/v1/completions"


def _overloaded() -> AiProviderRequestError:
    return AiProviderRequestError(
        "overloaded",
        status_code=529,
        provider_engine="claude",
        fallback_reason=AiFallbackReason.UNAVAILABLE,
    )


class _Engine(AIBaseCompletions):
    """A candidate that answers with its own name, or raises `fail`."""

    def __init__(
        self,
        name: str,
        *,
        fail: Exception | None = None,
        fail_on_prompt: str | None = None,
        family: str = "anthropic",
    ) -> None:
        super().__init__(model=name)
        self.fail = fail
        # Fail only this prompt, so one shared client serves some requests
        # itself and hands others to its fallback.
        self.fail_on_prompt = fail_on_prompt
        self.FALLBACK_ENGINE_FAMILY = family

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    @property
    def capabilities(self) -> AICompletionsCapabilitiesBase:
        return AICompletionsCapabilitiesBase(
            context_window_length=1000,
            supports_streaming=True,
            supports_tool_use=True,
            supports_structured_output=True,
            supports_async=True,
        )

    @property
    def list_model_names(self) -> list[str]:
        return [str(self.model)]

    @property
    def max_context_tokens(self) -> int:
        return 1000

    def send_prompt(self, prompt: str, **kwargs: Any) -> str:
        self._check()
        return f"{self.model}:text"

    async def asend_prompt(self, prompt: str, **kwargs: Any) -> str:
        # Yield first so concurrent requests really interleave on the loop.
        await asyncio.sleep(0.01)
        if prompt == self.fail_on_prompt:
            raise _overloaded()
        self._check()
        return f"{self.model}:text"

    def send_prompt_streaming(self, prompt: str, **kwargs: Any) -> Any:
        def _gen() -> Any:
            self._check()
            yield f"{self.model}-1"
            yield f"{self.model}-2"

        return _gen()

    def count_tokens(self, prompt: str, **kwargs: Any) -> int:
        return 1

    def strict_schema_prompt(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def send_structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def asend_structured_output(self, *args: Any, **kwargs: Any) -> Any:
        self._check()
        return AIStructuredOutputResult(
            data={"by": str(self.model)},
            finish_reason=AIFinishReason.COMPLETE,
            usage=AITokenUsage(input_tokens=10, output_tokens=5),
            raw_text="{}",
        )

    def send_conversation(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def asend_conversation(
        self, system_prompt: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> AITurnResult:
        self._check()
        return AITurnResult(
            text=f"{self.model}:turn",
            finish_reason=AIFinishReason.COMPLETE,
            raw_content=[{"type": "text", "text": "x"}],
            usage=AITokenUsage(input_tokens=10, output_tokens=5),
        )


def _chain(primary: _Engine, fallback: _Engine) -> AiFallbackCompletions:
    """The library's wrapper: claude:primary first, then openai:fallback."""
    return AiFallbackCompletions(
        primary=primary,
        primary_candidate=AIFallbackCandidate(engine="claude", model=primary.model),
        fallback_candidates=[
            AIFallbackCandidate(engine="openai", model=str(fallback.model))
        ],
        client_builder=lambda candidate: fallback,
    )


def _route_pool(wrapper: Any, plain: dict[tuple[str, str | None], Any]) -> Any:
    """Stand in for the pool: the wrapper for fallback=True, plain otherwise."""

    def _get(engine: str, model: str | None = None, *, fallback: bool = False) -> Any:
        return wrapper if fallback else plain[(engine, model)]

    return patch("ai_api_unified_http.routes_v1.get_completions_client", _get)


class TestPoolCarriesTheChain:
    """Which pooled clients get the chain, and what the chain holds."""

    @pytest.fixture(autouse=True)
    def _fresh_pool(self) -> Any:
        clients.reset_pools()
        yield
        clients.reset_pools()

    def test_fallback_clients_get_the_configured_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            COMPLETIONS_FALLBACKS_ENV,
            "openai:gpt-4o-mini,bedrock:amazon.nova-lite-v1:0",
        )
        with patch(
            "ai_api_unified_http.clients.AIFactory.get_ai_completions_client"
        ) as factory:
            clients.get_completions_client("claude", "claude-opus-5", fallback=True)

        chain = factory.call_args.kwargs["fallbacks"]
        # The Bedrock id keeps its own colon: the split is on the first one.
        assert [(c.engine, c.model) for c in chain] == [
            ("openai", "gpt-4o-mini"),
            ("bedrock", "amazon.nova-lite-v1:0"),
        ]

    def test_plain_clients_never_get_the_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Passing None would let the factory read the setting itself, so the
        # plain client has to ask for an empty chain explicitly.
        monkeypatch.setenv(COMPLETIONS_FALLBACKS_ENV, "openai:gpt-4o-mini")
        with patch(
            "ai_api_unified_http.clients.AIFactory.get_ai_completions_client"
        ) as factory:
            clients.get_completions_client("claude", "claude-opus-5")

        assert factory.call_args.kwargs["fallbacks"] == []

    def test_chain_and_plain_clients_are_separate_pool_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMPLETIONS_FALLBACKS_ENV, "openai:gpt-4o-mini")
        with patch(
            "ai_api_unified_http.clients.AIFactory.get_ai_completions_client",
            side_effect=lambda **_: MagicMock(),
        ):
            wrapped = clients.get_completions_client("claude", None, fallback=True)
            plain = clients.get_completions_client("claude", None)

        assert wrapped is not plain
        assert clients.pool_sizes()["completions"] == 2

    def test_the_requested_model_is_dropped_from_its_own_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Retrying an overloaded model on itself spends a second call on the
        # same failure.
        monkeypatch.setenv(
            COMPLETIONS_FALLBACKS_ENV,
            "openai:gpt-4o-mini,google-gemini:gemini-2.5-flash",
        )
        chain = clients.fallback_chain_for("OpenAI", "gpt-4o-mini")
        assert [c.label for c in chain] == ["google-gemini:gemini-2.5-flash"]

    def test_no_setting_means_no_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(COMPLETIONS_FALLBACKS_ENV, raising=False)
        assert clients.fallback_chain_for("claude", None) == []


class TestStartupCheck:
    def test_an_unknown_engine_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMPLETIONS_FALLBACKS_ENV, "opneai:gpt-4o-mini")
        with pytest.raises(FallbackMisconfiguredError, match="opneai"):
            verify_fallback_config()

    def test_an_unknown_reason_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMPLETIONS_FALLBACKS_ENV, "openai:gpt-4o-mini")
        monkeypatch.setenv(COMPLETIONS_FALLBACK_ON_ENV, "unavailable,overloaded")
        with pytest.raises(FallbackMisconfiguredError, match="overloaded"):
            verify_fallback_config()

    def test_a_valid_chain_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            COMPLETIONS_FALLBACKS_ENV, "openai:gpt-4o-mini,google-gemini"
        )
        monkeypatch.delenv(COMPLETIONS_FALLBACK_ON_ENV, raising=False)
        assert verify_fallback_config() == [
            "openai:gpt-4o-mini",
            "google-gemini:<default>",
        ]

    def test_no_chain_is_fine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(COMPLETIONS_FALLBACKS_ENV, raising=False)
        assert verify_fallback_config() == []


class TestCompletions:
    def test_a_fallback_answer_names_the_fallback(self, client: TestClient) -> None:
        wrapper = _chain(_Engine("opus", fail=_overloaded()), _Engine("mini"))
        with _route_pool(wrapper, {}):
            response = client.post(PATH, json={"engine": "claude", "prompt": "hi"})

        assert response.status_code == 200
        assert response.json() == {
            "text": "mini:text",
            "engine": "openai",
            "model": "mini",
            "served_by_fallback": True,
        }

    def test_a_primary_answer_reports_the_request(self, client: TestClient) -> None:
        wrapper = _chain(_Engine("opus"), _Engine("mini"))
        with _route_pool(wrapper, {}):
            body = client.post(
                PATH, json={"engine": "claude", "model": "opus", "prompt": "hi"}
            ).json()

        assert body["engine"] == "claude"
        assert body["model"] == "opus"
        assert body["served_by_fallback"] is False

    def test_fallback_false_asks_the_pool_for_the_plain_client(
        self, client: TestClient
    ) -> None:
        plain = _Engine("opus", fail=_overloaded())
        wrapper = _chain(_Engine("opus", fail=_overloaded()), _Engine("mini"))
        with _route_pool(wrapper, {("claude", None): plain}):
            response = client.post(
                PATH, json={"engine": "claude", "prompt": "hi", "fallback": False}
            )

        # The primary's failure reaches the caller, with its reason.
        assert response.status_code == 502
        assert response.json()["fallback_reason"] == "unavailable"

    def test_concurrent_requests_each_report_their_own_route(
        self, client: TestClient
    ) -> None:
        # One pooled client serves every request, interleaved on the loop. Its
        # "last route" belongs to whichever call finished last, which a stream
        # or a threadpool call can read after another request moved it. Each
        # request here has to report the route of its own call.
        shared = _chain(_Engine("opus", fail_on_prompt="bad"), _Engine("mini"))

        async def _both() -> list[dict[str, Any]]:
            import httpx

            transport = httpx.ASGITransport(app=client.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                headers=dict(client.headers),
            ) as http:
                responses = await asyncio.gather(
                    *[
                        http.post(
                            PATH,
                            json={"engine": "claude", "prompt": prompt},
                        )
                        for prompt in ["bad", "ok", "bad", "ok", "bad", "ok"]
                    ]
                )
            return [r.json() for r in responses]

        with _route_pool(shared, {}):
            bodies = asyncio.run(_both())

        assert [b["text"] for b in bodies] == ["mini:text", "opus:text"] * 3
        assert [b["engine"] for b in bodies] == ["openai", "claude"] * 3
        assert [b["served_by_fallback"] for b in bodies] == [True, False] * 3

    def test_a_stream_names_the_fallback_in_its_done_event(
        self, client: TestClient
    ) -> None:
        wrapper = _chain(_Engine("opus", fail=_overloaded()), _Engine("mini"))
        with _route_pool(wrapper, {}):
            response = client.post(
                PATH, json={"engine": "claude", "prompt": "hi", "stream": True}
            )

        frames = [
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        done = json.loads(frames[-1])
        assert done["engine"] == "openai"
        assert done["model"] == "mini"
        assert done["served_by_fallback"] is True
        assert "mini-1" in response.text

    def test_an_exhausted_chain_carries_the_last_reason(
        self, client: TestClient
    ) -> None:
        quota = AiProviderRequestError(
            "insufficient_quota",
            status_code=429,
            provider_engine="openai",
            fallback_reason=AiFallbackReason.QUOTA_EXHAUSTED,
        )
        wrapper = _chain(
            _Engine("opus", fail=_overloaded()), _Engine("mini", fail=quota)
        )
        with _route_pool(wrapper, {}):
            response = client.post(PATH, json={"engine": "claude", "prompt": "hi"})

        assert response.status_code == 429
        body = response.json()
        assert body["engine"] == "openai"
        assert body["fallback_reason"] == "quota_exhausted"


def _priced(model: str, input_per_1m: str) -> MagicMock:
    """A plain client whose rates the route prices with."""
    from decimal import Decimal

    plain = MagicMock()
    plain.capabilities.pricing.token_rates = object()
    plain.capabilities.pricing.compute_token_cost.side_effect = (
        lambda input_tokens, **_: Decimal(input_per_1m) * input_tokens / 1_000_000
    )
    return plain


class TestTypedResults:
    """Conversation and structured calls read the library's result stamp."""

    def test_a_structured_fallback_is_named_and_priced_at_the_fallback(
        self, client: TestClient
    ) -> None:
        wrapper = _chain(_Engine("opus", fail=_overloaded()), _Engine("mini"))
        plain = {("openai", "mini"): _priced("mini", "1")}
        with _route_pool(wrapper, plain):
            body = client.post(
                "/v1/structured",
                json={
                    "engine": "claude",
                    "prompt": "hi",
                    "response_schema": {"type": "object"},
                },
            ).json()

        assert body["data"] == {"by": "mini"}
        assert (body["engine"], body["model"]) == ("openai", "mini")
        assert body["served_by_fallback"] is True
        # 10 input tokens at $1 per million: priced at the fallback's rate.
        assert body["usd_cost"] == "0.00001"

    def test_a_conversation_fallback_is_named(self, client: TestClient) -> None:
        wrapper = _chain(_Engine("opus", fail=_overloaded()), _Engine("mini"))
        plain = {("openai", "mini"): _priced("mini", "1")}
        with _route_pool(wrapper, plain):
            body = client.post(
                "/v1/conversations/turn",
                json={
                    "engine": "claude",
                    "system_prompt": "s",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            ).json()

        assert body["text"] == "mini:turn"
        assert (body["engine"], body["model"]) == ("openai", "mini")
        assert body["served_by_fallback"] is True

    def test_a_primary_turn_keeps_the_requested_model(self, client: TestClient) -> None:
        wrapper = _chain(_Engine("opus"), _Engine("mini"))
        with _route_pool(wrapper, {}):
            body = client.post(
                "/v1/conversations/turn",
                json={
                    "engine": "claude",
                    "system_prompt": "s",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            ).json()

        # The request named no model, and the response says the same.
        assert (body["engine"], body["model"]) == ("claude", None)
        assert body["served_by_fallback"] is False
