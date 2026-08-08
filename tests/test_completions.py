# tests/test_completions.py

"""
The first live endpoint, buffered and streamed.

Every test patches the client pool rather than reaching a provider, so the
suite still runs with no keys and no network. What is under test is the
service's own behavior: what it passes to the library, what it returns, how it
rejects, and how a mid-stream failure is reported once the status line has
already gone out.
"""

import json
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ai_api_unified import AiProviderRequestError
from fastapi.testclient import TestClient

PATH: str = "/v1/completions"


@pytest.fixture
def fake_client() -> MagicMock:
    """Stand-in for a pooled completions client."""
    client = MagicMock()
    client.asend_prompt = AsyncMock(return_value="generated text")
    return client


@pytest.fixture
def pooled(fake_client: MagicMock) -> Iterator[MagicMock]:
    """Patch the pool so routes get the fake client."""
    with patch(
        "ai_api_unified_http.routes_v1.get_completions_client", return_value=fake_client
    ) as getter:
        yield getter


class TestBuffered:
    def test_returns_generated_text(
        self, client: TestClient, pooled: MagicMock, fake_client: MagicMock
    ) -> None:
        response = client.post(
            PATH, json={"engine": "claude", "model": "claude-opus-5", "prompt": "hi"}
        )
        assert response.status_code == 200
        assert response.json() == {
            "text": "generated text",
            "engine": "claude",
            "model": "claude-opus-5",
        }

    def test_pool_is_keyed_on_the_requested_engine_and_model(
        self, client: TestClient, pooled: MagicMock
    ) -> None:
        client.post(PATH, json={"engine": "openai", "model": "gpt-5.4", "prompt": "hi"})
        pooled.assert_called_once_with("openai", "gpt-5.4")

    def test_omitted_model_reaches_the_pool_as_none(
        self, client: TestClient, pooled: MagicMock
    ) -> None:
        client.post(PATH, json={"engine": "openai", "prompt": "hi"})
        pooled.assert_called_once_with("openai", None)

    def test_generation_options_are_forwarded(
        self, client: TestClient, pooled: MagicMock, fake_client: MagicMock
    ) -> None:
        client.post(
            PATH,
            json={
                "engine": "claude",
                "prompt": "hi",
                "system_prompt": "be terse",
                "max_response_tokens": 128,
                "request_timeout_seconds": 5.5,
            },
        )
        fake_client.asend_prompt.assert_awaited_once_with(
            "hi",
            system_prompt="be terse",
            max_response_tokens=128,
            request_timeout_seconds=5.5,
        )

    def test_provider_failure_maps_through_the_error_handlers(
        self, client: TestClient, pooled: MagicMock, fake_client: MagicMock
    ) -> None:
        fake_client.asend_prompt.side_effect = AiProviderRequestError(
            "rate limited", status_code=429, provider_engine="claude"
        )
        response = client.post(PATH, json={"engine": "claude", "prompt": "hi"})
        assert response.status_code == 429
        assert response.json()["error"] == "provider_rate_limited"

    def test_missing_engine_is_422_before_any_client_is_built(
        self, client: TestClient, pooled: MagicMock
    ) -> None:
        response = client.post(PATH, json={"prompt": "no engine"})
        assert response.status_code == 422
        pooled.assert_not_called()


class TestStreaming:
    @pytest.fixture
    def streaming_client(self, fake_client: MagicMock) -> MagicMock:
        fake_client.send_prompt_streaming = MagicMock(
            return_value=iter(["Hel", "lo", " world"])
        )
        return fake_client

    def test_streams_chunks_then_done(
        self, client: TestClient, streaming_client: MagicMock
    ) -> None:
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=streaming_client,
        ):
            response = client.post(
                PATH, json={"engine": "claude", "prompt": "hi", "stream": True}
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(response.text)
        assert [e["event"] for e in events] == ["chunk", "chunk", "chunk", "done"]
        assert "".join(e["data"]["text"] for e in events[:3]) == "Hello world"
        assert events[-1]["data"]["chunks"] == 3

    def test_empty_chunks_are_not_forwarded(
        self, client: TestClient, fake_client: MagicMock
    ) -> None:
        # Providers emit empty keep-alive chunks; forwarding them would show
        # as spurious empty updates in a browser client.
        fake_client.send_prompt_streaming = MagicMock(
            return_value=iter(["a", "", "b", ""])
        )
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=fake_client,
        ):
            response = client.post(
                PATH, json={"engine": "claude", "prompt": "hi", "stream": True}
            )

        events = _parse_sse(response.text)
        assert [e["event"] for e in events] == ["chunk", "chunk", "done"]

    def test_system_prompt_travels_through_other_params(
        self, client: TestClient, streaming_client: MagicMock
    ) -> None:
        # There is no system_prompt keyword on send_prompt_streaming; it only
        # reaches the call inside other_params.
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=streaming_client,
        ):
            client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "hi",
                    "system_prompt": "be terse",
                    "stream": True,
                },
            )
        _, kwargs = streaming_client.send_prompt_streaming.call_args
        assert kwargs["other_params"].system_prompt == "be terse"

    def test_no_system_prompt_sends_no_params(
        self, client: TestClient, streaming_client: MagicMock
    ) -> None:
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=streaming_client,
        ):
            client.post(PATH, json={"engine": "claude", "prompt": "hi", "stream": True})
        _, kwargs = streaming_client.send_prompt_streaming.call_args
        assert kwargs["other_params"] is None

    @pytest.mark.parametrize(
        "field,value", [("max_response_tokens", 128), ("request_timeout_seconds", 5.0)]
    )
    def test_fields_the_streaming_call_cannot_honor_are_rejected(
        self, client: TestClient, streaming_client: MagicMock, field: str, value: object
    ) -> None:
        # Accepting and ignoring them would let a caller cap their spend in a
        # request that silently has no cap.
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=streaming_client,
        ):
            response = client.post(
                PATH,
                json={
                    "engine": "claude",
                    "prompt": "hi",
                    "stream": True,
                    field: value,
                },
            )
        assert response.status_code == 400
        assert field in response.json()["detail"]

    def test_the_same_fields_are_accepted_when_not_streaming(
        self, client: TestClient, pooled: MagicMock
    ) -> None:
        response = client.post(
            PATH,
            json={
                "engine": "claude",
                "prompt": "hi",
                "max_response_tokens": 128,
                "request_timeout_seconds": 5.0,
            },
        )
        assert response.status_code == 200

    def test_midstream_failure_arrives_as_a_terminal_error_event(
        self, client: TestClient, fake_client: MagicMock
    ) -> None:
        # The 200 status line is already sent by the time this fails, so the
        # failure cannot become a 502. It has to travel in-band.
        def explode() -> Iterator[str]:
            yield "partial"
            raise AiProviderRequestError(
                "upstream died", status_code=500, provider_engine="claude"
            )

        fake_client.send_prompt_streaming = MagicMock(return_value=explode())
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=fake_client,
        ):
            response = client.post(
                PATH, json={"engine": "claude", "prompt": "hi", "stream": True}
            )

        assert response.status_code == 200
        events = _parse_sse(response.text)
        assert [e["event"] for e in events] == ["chunk", "error"]
        assert events[-1]["data"]["error"] == "stream_failed"
        assert events[-1]["data"]["chunks_delivered"] == 1

    def test_failure_before_the_first_chunk_still_returns_200_with_an_error_event(
        self, client: TestClient, fake_client: MagicMock
    ) -> None:
        def explode() -> Iterator[str]:
            raise AiProviderRequestError("died", status_code=500)
            yield  # pragma: no cover - unreachable, marks this a generator

        fake_client.send_prompt_streaming = MagicMock(return_value=explode())
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=fake_client,
        ):
            response = client.post(
                PATH, json={"engine": "claude", "prompt": "hi", "stream": True}
            )

        events = _parse_sse(response.text)
        assert [e["event"] for e in events] == ["error"]
        assert events[0]["data"]["chunks_delivered"] == 0

    def test_streaming_response_disables_proxy_buffering(
        self, client: TestClient, streaming_client: MagicMock
    ) -> None:
        # A buffering proxy defeats streaming entirely; nginx needs telling.
        with patch(
            "ai_api_unified_http.routes_v1.get_completions_client",
            return_value=streaming_client,
        ):
            response = client.post(
                PATH, json={"engine": "claude", "prompt": "hi", "stream": True}
            )
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["cache-control"] == "no-cache"


def test_streaming_requires_a_key_like_every_other_route(
    anonymous_client: TestClient,
) -> None:
    response = anonymous_client.post(
        PATH, json={"engine": "claude", "prompt": "hi", "stream": True}
    )
    assert response.status_code == 401


def _parse_sse(body: str) -> list[dict]:
    """Parse an SSE body into a list of {event, data} dicts."""
    events: list[dict] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        event: str = ""
        data: str = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        events.append({"event": event, "data": json.loads(data)})
    return events
