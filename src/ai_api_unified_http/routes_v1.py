# src/ai_api_unified_http/routes_v1.py

"""
v1 API routes.

Every model-invoking endpoint returns HTTP 501 until its implementation
lands; the request schemas are real so the OpenAPI spec and generated
TypeScript client are stable from day one. The endpoint-to-library-call
mapping lives in docs/technical-design.md.
"""

from collections.abc import Iterator
from typing import Any

from ai_api_unified import AICompletionsPromptParamsBase, AITool
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .clients import get_completions_client
from .conversation_token import (
    InvalidConversationTokenError,
    decode_conversation_token,
    encode_conversation_token,
    looks_like_conversation_token,
)
from .schemas import (
    CompletionRequest,
    CompletionResponse,
    ConversationTurnRequest,
    ConversationTurnResponse,
    EmbeddingsRequest,
    ErrorResponse,
    NotImplementedResponse,
    StructuredRequest,
    StructuredResponse,
    TokenCountRequest,
    TokenUsage,
    ToolCall,
)
from .streaming import SSE_HEADERS, SSE_MEDIA_TYPE, sse_from_sync_iterator

router: APIRouter = APIRouter(prefix="/v1")

_NOT_IMPLEMENTED_STATUS: int = 501

# send_prompt_streaming takes only the prompt and other_params. These fields
# have nowhere to go on the streaming path, so a request that sets them is
# rejected rather than served while silently ignoring them.
_FIELDS_UNSUPPORTED_WHEN_STREAMING: tuple[str, ...] = (
    "max_response_tokens",
    "request_timeout_seconds",
)


def _usage(usage: Any) -> TokenUsage:
    """Convert the library's token usage into the response model."""
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        total_tokens=usage.total_tokens,
    )


def _tools(tools: list[Any] | None) -> list[AITool] | None:
    """Convert caller tool schemas into library tools.

    These are schemas describing tools the caller will run, never executable
    code, so nothing here is invoked by the service.
    """
    if not tools:
        return None
    return [
        AITool(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
            strict=tool.strict,
        )
        for tool in tools
    ]


def _expand_conversation_tokens(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace conversation tokens in assistant messages with their content.

    The caller places each token where the turn actually occurred, because
    only they know where a new user message belongs relative to it. Appending
    the turn for them would put it after the new user message and reorder the
    conversation.

    Args:
        messages: Caller history, possibly containing assistant messages whose
            content is a conversation token.

    Returns:
        list[dict[str, Any]]: History with tokens decoded in place.

    Raises:
        InvalidConversationTokenError: When a token cannot be decoded.
    """
    expanded: list[dict[str, Any]] = []
    for message in messages:
        content: Any = message.get("content")
        is_token: bool = message.get(
            "role"
        ) == "assistant" and looks_like_conversation_token(content)
        if is_token:
            expanded.append({**message, "content": decode_conversation_token(content)})
        else:
            expanded.append(message)
    return expanded


def _optional(**kwargs: Any) -> dict[str, Any]:
    """Drop None values so library defaults apply.

    asend_structured_output defaults max_response_tokens to 2048 rather than
    None, so passing None explicitly would override a working default with an
    invalid value.
    """
    return {key: value for key, value in kwargs.items() if value is not None}


def _not_implemented(endpoint: str) -> JSONResponse:
    """Uniform 501 body for scaffolded endpoints."""
    body = NotImplementedResponse(endpoint=endpoint)
    return JSONResponse(status_code=_NOT_IMPLEMENTED_STATUS, content=body.model_dump())


def _reject_unsupported_streaming_fields(request: CompletionRequest) -> None:
    """Reject streaming requests carrying fields the library cannot honor.

    Raises:
        HTTPException: 400 naming each unsupported field that was set.
    """
    offenders: list[str] = [
        field
        for field in _FIELDS_UNSUPPORTED_WHEN_STREAMING
        if getattr(request, field) is not None
    ]
    if not offenders:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"{', '.join(offenders)} cannot be applied to a streaming completion: "
            f"the library's streaming call accepts only the prompt and a system "
            f"prompt. Drop the field, or set stream=false to use it."
        ),
    )


@router.post(
    "/completions",
    response_model=None,
    responses={
        200: {
            "model": CompletionResponse,
            "description": (
                "Buffered completion. With stream=true the response is instead "
                "text/event-stream carrying chunk events, then one terminal "
                "done or error event."
            ),
        },
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Text completion (buffered or SSE stream)",
)
async def completions(
    request: CompletionRequest,
) -> CompletionResponse | StreamingResponse:
    """Generate a completion, buffered by default or streamed over SSE.

    Args:
        request: Engine selection, prompt, and generation options.

    Returns:
        CompletionResponse | StreamingResponse: The completion text, or an SSE
            stream when `stream` is true.
    """
    client = get_completions_client(request.engine, request.model)

    if request.stream:
        _reject_unsupported_streaming_fields(request)
        # system_prompt reaches the streaming call only through other_params;
        # there is no keyword for it on send_prompt_streaming.
        params: AICompletionsPromptParamsBase | None = (
            AICompletionsPromptParamsBase(system_prompt=request.system_prompt)
            if request.system_prompt
            else None
        )
        chunks: Iterator[str] = client.send_prompt_streaming(
            request.prompt, other_params=params
        )
        return StreamingResponse(
            sse_from_sync_iterator(chunks, request.engine, request.model),
            media_type=SSE_MEDIA_TYPE,
            headers=SSE_HEADERS,
        )

    text: str = await client.asend_prompt(
        request.prompt,
        system_prompt=request.system_prompt,
        max_response_tokens=request.max_response_tokens,
        request_timeout_seconds=request.request_timeout_seconds,
    )
    return CompletionResponse(text=text, engine=request.engine, model=request.model)


@router.post(
    "/structured",
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Schema-validated structured output",
)
async def structured(request: StructuredRequest) -> StructuredResponse:
    """Generate output validated against a caller-supplied JSON schema.

    Args:
        request: Engine selection, prompt or messages, and the response schema.

    Returns:
        StructuredResponse: Parsed data with the finish reason and usage. Data
            is null on any finish reason other than `complete`.
    """
    if request.prompt is None and not request.messages:
        raise HTTPException(
            status_code=400,
            detail="Provide either prompt or messages.",
        )

    client = get_completions_client(request.engine, request.model)
    result = await client.asend_structured_output(
        request.prompt,
        response_schema=request.response_schema,
        system_prompt=request.system_prompt,
        messages=request.messages,
        **_optional(max_response_tokens=request.max_response_tokens),
        request_timeout_seconds=request.request_timeout_seconds,
    )
    return StructuredResponse(
        data=result.data,
        finish_reason=result.finish_reason.value,
        usage=_usage(result.usage),
        raw_text=result.raw_text,
        engine=request.engine,
        model=request.model,
    )


@router.post(
    "/conversations/turn",
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="One stateless conversation turn, with optional tool schemas",
)
async def conversation_turn(
    request: ConversationTurnRequest,
) -> ConversationTurnResponse:
    """Run one assistant turn; the caller owns history and tool execution.

    Args:
        request: System prompt, full history, optional tools, and the previous
            turn's opaque token.

    Returns:
        ConversationTurnResponse: The assistant turn, any tool calls the caller
            must execute, and the token to echo back next turn.
    """
    try:
        messages: list[dict[str, Any]] = _expand_conversation_tokens(request.messages)
    except InvalidConversationTokenError as error:
        # Caller-fixable, and it must fail before the pool is touched so a bad
        # token costs no provider call.
        raise HTTPException(status_code=400, detail=str(error)) from error

    client = get_completions_client(request.engine, request.model)
    turn = await client.asend_conversation(
        request.system_prompt,
        messages,
        tools=_tools(request.tools),
        tool_choice=request.tool_choice,
        max_response_tokens=request.max_response_tokens,
        request_timeout_seconds=request.request_timeout_seconds,
    )
    return ConversationTurnResponse(
        text=turn.text,
        tool_calls=[
            ToolCall(id=call.id, name=call.name, input=call.input)
            for call in turn.tool_calls
        ],
        finish_reason=turn.finish_reason.value,
        usage=_usage(turn.usage),
        conversation_token=encode_conversation_token(turn.raw_content),
        engine=request.engine,
        model=request.model,
    )


@router.post(
    "/embeddings",
    responses={501: {"model": NotImplementedResponse}},
    summary="Embedding vectors for one or more inputs",
)
def embeddings(request: EmbeddingsRequest) -> JSONResponse:
    """Will map to agenerate_embeddings / agenerate_embeddings_batch."""
    return _not_implemented("/v1/embeddings")


@router.post(
    "/tokens/count",
    responses={501: {"model": NotImplementedResponse}},
    summary="Provider-side token count for a prompt",
)
def token_count(request: TokenCountRequest) -> JSONResponse:
    """Will map to count_tokens (sync; served from the threadpool)."""
    return _not_implemented("/v1/tokens/count")


@router.get(
    "/models",
    responses={501: {"model": NotImplementedResponse}},
    summary="Catalogued models with capabilities, pricing, and lifecycle",
)
def models() -> JSONResponse:
    """Will surface list_model_names, capabilities, and the pricing registry."""
    return _not_implemented("/v1/models")
