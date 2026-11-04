# tests/test_tool_results.py

"""
Neutral tool results on /v1/conversations/turn.

A caller sends every tool result in one shape, and the service builds the
serving engine's own message from it. The fakes below delegate to each
library engine's real builder, so a change to an engine's wire shape shows
up here instead of at the provider.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ai_api_unified import (
    AIFallbackCandidate,
    AiFallbackCompletions,
    AIFinishReason,
    AITokenUsage,
    AITurnResult,
)
from ai_api_unified.completions.ai_anthropic_completions import AiAnthropicCompletions
from ai_api_unified.completions.ai_google_gemini_completions import (
    GoogleGeminiCompletions,
)
from ai_api_unified.completions.ai_openai_completions import AiOpenAICompletions
from ai_api_unified.completions.ai_openai_responses_completions import (
    AiOpenAIResponsesCompletions,
)
from fastapi.testclient import TestClient

from ai_api_unified_http.conversation_token import encode_turn_messages
from ai_api_unified_http.tool_results import (
    InvalidToolResultError,
    is_neutral_tool_result,
    shape_tool_results,
    validate_tool_results,
)

TURN: str = "/v1/conversations/turn"
USAGE = AITokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)

# Each engine's real builder. None of them reads the client.
BUILDERS: dict[str, Any] = {
    "claude": AiAnthropicCompletions._build_tool_result_message_provider,
    "openai": AiOpenAICompletions._build_tool_result_message_provider,
    "openai-responses": AiOpenAIResponsesCompletions._build_tool_result_message_provider,
    "google-gemini": GoogleGeminiCompletions._build_tool_result_message_provider,
}


def _engine(name: str) -> MagicMock:
    """A plain engine client whose tool results come from the real builder."""
    client = MagicMock()
    client.build_tool_result_message.side_effect = lambda **kwargs: BUILDERS[name](
        None, **kwargs
    )
    client.asend_conversation = AsyncMock(
        return_value=AITurnResult(
            text="done",
            finish_reason=AIFinishReason.COMPLETE,
            raw_content=[{"type": "text", "text": "done"}],
            usage=USAGE,
        )
    )
    client.extend_messages_with_turn.side_effect = lambda messages, turn: [
        *messages,
        {"role": "assistant", "content": turn.raw_content},
    ]
    return client


def _result(call_id: str, output: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_call_id": call_id,
        "result": {"output": output},
        "is_error": is_error,
    }


CLAUDE_TOOL_TURN: dict[str, Any] = {
    "role": "assistant",
    "content": [
        {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
        {"type": "tool_use", "id": "t2", "name": "read", "input": {}},
    ],
}


class TestShapes:
    def test_claude_takes_a_turns_results_in_one_user_message(self) -> None:
        shaped = shape_tool_results(
            _engine("claude"),
            [CLAUDE_TOOL_TURN, _result("t1", "a"), _result("t2", "b", is_error=True)],
        )

        assert shaped[1:] == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": json.dumps({"output": "a"}),
                        "is_error": False,
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": json.dumps({"output": "b"}),
                        "is_error": True,
                    },
                ],
            }
        ]

    def test_gemini_takes_a_turns_responses_in_one_message(self) -> None:
        shaped = shape_tool_results(
            _engine("google-gemini"),
            [{"role": "model", "parts": []}, _result("read", "a"), _result("ls", "b")],
        )

        assert shaped[1:] == [
            {
                "role": "user",
                "parts": [
                    {
                        "function_response": {
                            "name": "read",
                            "response": {"output": "a"},
                        }
                    },
                    {"function_response": {"name": "ls", "response": {"output": "b"}}},
                ],
            }
        ]

    def test_openai_chat_takes_one_tool_message_per_result(self) -> None:
        shaped = shape_tool_results(
            _engine("openai"), [_result("c1", "a"), _result("c2", "b", is_error=True)]
        )

        assert shaped == [
            {"role": "tool", "tool_call_id": "c1", "content": '{"output": "a"}'},
            {
                "role": "tool",
                "tool_call_id": "c2",
                "content": '{"error": {"output": "b"}}',
            },
        ]

    def test_openai_responses_takes_one_item_per_result(self) -> None:
        shaped = shape_tool_results(
            _engine("openai-responses"), [_result("c1", "a"), _result("c2", "b")]
        )

        assert [item["type"] for item in shaped] == ["function_call_output"] * 2
        assert [item["call_id"] for item in shaped] == ["c1", "c2"]

    def test_results_of_different_turns_stay_apart(self) -> None:
        # A user message between two runs ends the first run.
        shaped = shape_tool_results(
            _engine("claude"),
            [
                _result("t1", "a"),
                {"role": "user", "content": "next"},
                _result("t2", "b"),
            ],
        )

        assert len(shaped) == 3
        assert shaped[1] == {"role": "user", "content": "next"}

    def test_a_result_already_in_an_engines_shape_passes_through(self) -> None:
        native = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}],
        }
        client = _engine("claude")

        assert shape_tool_results(client, [native, _result("t2", "b")])[0] is native
        # A neutral result is never merged into a message the caller built.
        assert len(shape_tool_results(client, [native, _result("t2", "b")])) == 2

    def test_a_history_without_neutral_results_is_unchanged(self) -> None:
        history = [{"role": "user", "content": "hi"}, CLAUDE_TOOL_TURN]
        client = _engine("claude")

        assert shape_tool_results(client, history) == history
        client.build_tool_result_message.assert_not_called()


class TestFallback:
    def test_the_chain_is_given_the_history_before_each_result(self) -> None:
        chain = MagicMock(spec=AiFallbackCompletions)
        chain.build_tool_result_message.side_effect = lambda **kwargs: BUILDERS[
            "openai"
        ](
            None,
            tool_call_id=kwargs["tool_call_id"],
            result=kwargs["result"],
            is_error=kwargs["is_error"],
        )
        turn = {"role": "assistant", "content": None, "tool_calls": []}

        shape_tool_results(chain, [turn, _result("c1", "a"), _result("c2", "b")])

        histories = [
            call.kwargs["messages"]
            for call in chain.build_tool_result_message.call_args_list
        ]
        assert histories[0] == [turn]
        assert histories[1][0] == turn and histories[1][1]["tool_call_id"] == "c1"

    def test_a_real_chain_builds_in_the_shape_the_history_came_from(self) -> None:
        # The primary is Claude, but the turn being answered came from the
        # OpenAI fallback, so its results take OpenAI's shape.
        chain = AiFallbackCompletions(
            primary=_real_engine(AiAnthropicCompletions, "opus", "anthropic"),
            primary_candidate=AIFallbackCandidate(engine="claude", model="opus"),
            fallback_candidates=[AIFallbackCandidate(engine="openai", model="mini")],
            client_builder=lambda candidate: _real_engine(
                AiOpenAICompletions, "mini", "openai"
            ),
        )
        # The earlier turn failed over, which built the fallback. The chain
        # matches a history only to an engine it has built.
        chain._client_at(1)
        openai_turn = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function"}],
        }

        shaped = shape_tool_results(chain, [openai_turn, _result("c1", "a")])

        assert shaped[1] == {
            "role": "tool",
            "tool_call_id": "c1",
            "content": '{"output": "a"}',
        }


def _real_engine(cls: type, model: str, family: str) -> Any:
    """A library engine object that never calls its provider.

    Built without __init__, so it needs no credentials. Only the parts the
    chain reads to build a tool result are set.
    """
    engine = cls.__new__(cls)
    engine.model = model
    engine.FALLBACK_ENGINE_FAMILY = family
    engine._require_tool_use_capability = lambda: None
    return engine


class TestValidation:
    @pytest.mark.parametrize(
        ("message", "complaint"),
        [
            ({"type": "tool_result", "result": {}}, "tool_call_id"),
            ({"type": "tool_result", "tool_call_id": "", "result": {}}, "tool_call_id"),
            ({"type": "tool_result", "tool_call_id": "c", "result": "text"}, "result"),
            (
                {
                    "type": "tool_result",
                    "tool_call_id": "c",
                    "result": {},
                    "is_error": 1,
                },
                "is_error",
            ),
            (
                {"type": "tool_result", "tool_call_id": "c", "result": {}, "extra": 1},
                "unknown fields",
            ),
        ],
    )
    def test_a_malformed_result_names_the_problem(
        self, message: dict[str, Any], complaint: str
    ) -> None:
        with pytest.raises(InvalidToolResultError, match=complaint):
            validate_tool_results([{"role": "user", "content": "hi"}, message])

    def test_is_error_defaults_to_false(self) -> None:
        shaped = shape_tool_results(
            _engine("claude"),
            [{"type": "tool_result", "tool_call_id": "t1", "result": {"x": 1}}],
        )
        assert shaped[0]["content"][0]["is_error"] is False

    def test_only_a_role_less_tool_result_is_neutral(self) -> None:
        assert is_neutral_tool_result(_result("c", "a"))
        # A Claude content block, or a message with a role, is not.
        assert not is_neutral_tool_result({"role": "user", "type": "tool_result"})
        assert not is_neutral_tool_result({"type": "function_call_output"})


class TestRoute:
    def test_the_engine_receives_its_own_shape(self, client: TestClient) -> None:
        engine = _engine("claude")
        token = encode_turn_messages([CLAUDE_TOOL_TURN])
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client", return_value=engine
        ):
            response = client.post(
                TURN,
                json={
                    "engine": "claude",
                    "system_prompt": "s",
                    "messages": [
                        {"role": "user", "content": "read two files"},
                        {"role": "assistant", "content": token},
                        _result("t1", "a"),
                        _result("t2", "b"),
                    ],
                },
            )

        assert response.status_code == 200
        args, _ = engine.asend_conversation.call_args
        sent = args[1]
        assert sent[1] == CLAUDE_TOOL_TURN
        assert [block["tool_use_id"] for block in sent[2]["content"]] == ["t1", "t2"]
        assert len(sent) == 3

    def test_a_malformed_result_is_400_before_the_pool(
        self, client: TestClient
    ) -> None:
        with patch("ai_api_unified_http.routes_v1.get_completions_client") as getter:
            response = client.post(
                TURN,
                json={
                    "engine": "claude",
                    "system_prompt": "s",
                    "messages": [{"type": "tool_result", "result": {}}],
                },
            )

        assert response.status_code == 400
        assert "tool_call_id" in response.json()["detail"]
        getter.assert_not_called()
