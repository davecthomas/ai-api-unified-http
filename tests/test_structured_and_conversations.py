# tests/test_structured_and_conversations.py

"""
The two endpoints that return typed results.

Both carry usage and a finish reason natively, so the tests care about what
the service does with them: surfacing a null `data` honestly, converting tool
schemas without executing anything, and round-tripping provider content
through an opaque token the caller never has to understand.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ai_api_unified import (
    AIFinishReason,
    AIStructuredOutputResult,
    AITokenUsage,
    AIToolCall,
    AITurnResult,
)
from fastapi.testclient import TestClient

from ai_api_unified_http.conversation_token import (
    InvalidConversationTokenError,
    decode_conversation_token,
    encode_conversation_token,
)

STRUCTURED: str = "/v1/structured"
TURN: str = "/v1/conversations/turn"

USAGE = AITokenUsage(
    input_tokens=10, output_tokens=5, cached_input_tokens=0, total_tokens=15
)


@pytest.fixture
def fake_client() -> MagicMock:
    client = MagicMock()
    client.asend_structured_output = AsyncMock(
        return_value=AIStructuredOutputResult(
            data={"name": "Jane"},
            finish_reason=AIFinishReason.COMPLETE,
            usage=USAGE,
            raw_text='{"name": "Jane"}',
        )
    )
    client.asend_conversation = AsyncMock(
        return_value=AITurnResult(
            text="hello",
            tool_calls=[],
            finish_reason=AIFinishReason.COMPLETE,
            raw_content=[{"type": "text", "text": "hello"}],
            usage=USAGE,
        )
    )
    return client


@pytest.fixture
def pooled(fake_client: MagicMock):
    with patch(
        "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake_client
    ) as getter:
        yield getter


class TestStructured:
    def test_returns_parsed_data_with_usage(
        self, client: TestClient, pooled: MagicMock
    ) -> None:
        response = client.post(
            STRUCTURED,
            json={
                "engine": "openai",
                "prompt": "Extract the name: Jane",
                "response_schema": {"type": "object"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["data"] == {"name": "Jane"}
        assert body["finish_reason"] == "complete"
        assert body["usage"]["input_tokens"] == 10
        assert body["raw_text"] == '{"name": "Jane"}'

    @pytest.mark.parametrize("reason", [AIFinishReason.LENGTH, AIFinishReason.REFUSAL])
    def test_null_data_is_reported_with_its_reason(
        self, client: TestClient, fake_client: MagicMock, reason: AIFinishReason
    ) -> None:
        # A truncated or refused response must not read as an empty result.
        # raw_text stays populated so the caller can see what came back.
        fake_client.asend_structured_output.return_value = AIStructuredOutputResult(
            data=None, finish_reason=reason, usage=USAGE, raw_text='{"partial'
        )
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=fake_client,
        ):
            response = client.post(
                STRUCTURED,
                json={
                    "engine": "openai",
                    "prompt": "x",
                    "response_schema": {"type": "object"},
                },
            )
        body = response.json()
        assert response.status_code == 200
        assert body["data"] is None
        assert body["finish_reason"] == reason.value
        assert body["raw_text"] == '{"partial'

    def test_neither_prompt_nor_messages_is_400(
        self, client: TestClient, pooled: MagicMock
    ) -> None:
        response = client.post(
            STRUCTURED, json={"engine": "openai", "response_schema": {}}
        )
        assert response.status_code == 400

    def test_messages_alone_is_accepted(
        self, client: TestClient, pooled: MagicMock
    ) -> None:
        response = client.post(
            STRUCTURED,
            json={
                "engine": "openai",
                "messages": [{"role": "user", "content": "hi"}],
                "response_schema": {},
            },
        )
        assert response.status_code == 200

    def test_omitted_token_budget_does_not_override_the_library_default(
        self, client: TestClient, pooled: MagicMock, fake_client: MagicMock
    ) -> None:
        # asend_structured_output defaults max_response_tokens to 2048, not
        # None. Forwarding None would replace a working default with an
        # invalid value.
        client.post(
            STRUCTURED,
            json={"engine": "openai", "prompt": "x", "response_schema": {}},
        )
        _, kwargs = fake_client.asend_structured_output.call_args
        assert "max_response_tokens" not in kwargs

    def test_explicit_token_budget_is_forwarded(
        self, client: TestClient, pooled: MagicMock, fake_client: MagicMock
    ) -> None:
        client.post(
            STRUCTURED,
            json={
                "engine": "openai",
                "prompt": "x",
                "response_schema": {},
                "max_response_tokens": 512,
            },
        )
        _, kwargs = fake_client.asend_structured_output.call_args
        assert kwargs["max_response_tokens"] == 512


class TestConversationTurn:
    def test_returns_text_usage_and_a_token(
        self, client: TestClient, pooled: MagicMock
    ) -> None:
        response = client.post(
            TURN, json={"engine": "claude", "system_prompt": "s", "messages": []}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["text"] == "hello"
        assert body["finish_reason"] == "complete"
        assert body["usage"]["total_tokens"] == 15
        assert body["conversation_token"]

    def test_tool_calls_are_surfaced_for_the_caller_to_execute(
        self, client: TestClient, fake_client: MagicMock
    ) -> None:
        # The service never runs a tool. It reports what the model asked for
        # and the caller executes it inside their own trust boundary.
        fake_client.asend_conversation.return_value = AITurnResult(
            text=None,
            tool_calls=[AIToolCall(id="call_1", name="lookup", input={"q": "weather"})],
            finish_reason=AIFinishReason.TOOL_USE,
            raw_content=[{"type": "tool_use", "id": "call_1"}],
            usage=USAGE,
        )
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=fake_client,
        ):
            response = client.post(
                TURN, json={"engine": "claude", "system_prompt": "s", "messages": []}
            )
        body = response.json()
        assert body["finish_reason"] == "tool_use"
        assert body["tool_calls"] == [
            {"id": "call_1", "name": "lookup", "input": {"q": "weather"}}
        ]

    def test_tool_schemas_are_converted_without_being_executed(
        self, client: TestClient, pooled: MagicMock, fake_client: MagicMock
    ) -> None:
        client.post(
            TURN,
            json={
                "engine": "claude",
                "system_prompt": "s",
                "messages": [],
                "tools": [
                    {
                        "name": "lookup",
                        "description": "look something up",
                        "input_schema": {"type": "object"},
                    }
                ],
                "tool_choice": "auto",
            },
        )
        _, kwargs = fake_client.asend_conversation.call_args
        assert [tool.name for tool in kwargs["tools"]] == ["lookup"]
        assert kwargs["tool_choice"] == "auto"

    def test_no_tools_sends_none_rather_than_an_empty_list(
        self, client: TestClient, pooled: MagicMock, fake_client: MagicMock
    ) -> None:
        client.post(
            TURN, json={"engine": "claude", "system_prompt": "s", "messages": []}
        )
        _, kwargs = fake_client.asend_conversation.call_args
        assert kwargs["tools"] is None

    def test_a_token_is_decoded_in_the_position_the_caller_placed_it(
        self, client: TestClient, pooled: MagicMock, fake_client: MagicMock
    ) -> None:
        # Ordering is the caller's, because only they know where a new user
        # message belongs relative to the previous assistant turn. Appending
        # for them would produce [user, user, assistant] and reorder the
        # conversation.
        content = [{"type": "text", "text": "earlier reply"}]
        token = encode_conversation_token(content)

        client.post(
            TURN,
            json={
                "engine": "claude",
                "system_prompt": "s",
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": token},
                    {"role": "user", "content": "follow up"},
                ],
            },
        )
        args, _ = fake_client.asend_conversation.call_args
        sent = args[1]
        assert sent[0] == {"role": "user", "content": "first"}
        assert sent[1] == {"role": "assistant", "content": content}
        assert sent[2] == {"role": "user", "content": "follow up"}

    def test_ordinary_assistant_text_is_left_alone(
        self, client: TestClient, pooled: MagicMock, fake_client: MagicMock
    ) -> None:
        client.post(
            TURN,
            json={
                "engine": "claude",
                "system_prompt": "s",
                "messages": [{"role": "assistant", "content": "just text"}],
            },
        )
        args, _ = fake_client.asend_conversation.call_args
        assert args[1][0]["content"] == "just text"

    def test_a_token_shaped_user_message_is_not_decoded(
        self, client: TestClient, pooled: MagicMock, fake_client: MagicMock
    ) -> None:
        # Only assistant turns carry tokens. A user quoting one is text.
        token = encode_conversation_token([{"type": "text", "text": "x"}])
        client.post(
            TURN,
            json={
                "engine": "claude",
                "system_prompt": "s",
                "messages": [{"role": "user", "content": token}],
            },
        )
        args, _ = fake_client.asend_conversation.call_args
        assert args[1][0]["content"] == token

    @pytest.mark.parametrize(
        "bad_token", ["v1.!!!not-base64!!!", "v99.eyJhIjogMX0=", "v1."]
    )
    def test_an_unusable_token_is_400_not_a_provider_call(
        self, client: TestClient, pooled: MagicMock, bad_token: str
    ) -> None:
        # Caller-fixable, and it must fail before spending money on a provider
        # call built from content the service could not decode. A v99 token is
        # rejected rather than passed through as literal assistant text.
        response = client.post(
            TURN,
            json={
                "engine": "claude",
                "system_prompt": "s",
                "messages": [{"role": "assistant", "content": bad_token}],
            },
        )
        assert response.status_code == 400
        pooled.assert_not_called()

    def test_a_turn_with_no_content_omits_the_token(
        self, client: TestClient, fake_client: MagicMock
    ) -> None:
        fake_client.asend_conversation.return_value = AITurnResult(
            text="hi",
            tool_calls=[],
            finish_reason=AIFinishReason.COMPLETE,
            raw_content=None,
            usage=USAGE,
        )
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=fake_client,
        ):
            response = client.post(
                TURN, json={"engine": "claude", "system_prompt": "s", "messages": []}
            )
        assert response.json()["conversation_token"] is None


class TestConversationToken:
    def test_round_trips_provider_content(self) -> None:
        content = [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "x"}]
        assert decode_conversation_token(encode_conversation_token(content)) == content

    def test_none_content_produces_no_token(self) -> None:
        assert encode_conversation_token(None) is None

    def test_token_is_opaque_rather_than_readable_json(self) -> None:
        # If the token were plain JSON, clients would parse it and a provider's
        # internal shape would become this service's public contract.
        token = encode_conversation_token([{"type": "text", "text": "secret-ish"}])
        assert "text" not in token
        assert token.startswith("v1.")

    def test_a_future_version_is_rejected_clearly(self) -> None:
        # The version prefix exists so an old token fails with a message
        # instead of being replayed to a provider as malformed content.
        with pytest.raises(InvalidConversationTokenError) as caught:
            decode_conversation_token("v2.eyJhIjogMX0=")
        assert "new conversation" in str(caught.value)

    @pytest.mark.parametrize("bad", ["no-separator", "v1.@@@", "v1.aGVsbG8="])
    def test_malformed_tokens_raise(self, bad: str) -> None:
        with pytest.raises(InvalidConversationTokenError):
            decode_conversation_token(bad)
