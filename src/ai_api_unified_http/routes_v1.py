# src/ai_api_unified_http/routes_v1.py

"""
v1 API routes.

Every model-invoking endpoint returns HTTP 501 until its implementation
lands; the request schemas are real so the OpenAPI spec and generated
TypeScript client are stable from day one. The endpoint-to-library-call
mapping lives in docs/technical-design.md.
"""

import logging
from collections.abc import Iterator
from typing import Any

from ai_api_unified import (
    AICompletionsPromptParamsBase,
    AIFactory,
    AiProviderCapabilityUnsupportedError,
    AITool,
    get_model_info,
)
from ai_api_unified.pricing import DICT_MODEL_INFO
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .clients import get_completions_client, get_embeddings_client
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
    EmbeddingsResponse,
    EmbeddingVector,
    ErrorResponse,
    ModelInfo,
    ModelPricing,
    ModelsResponse,
    NotImplementedResponse,
    StructuredRequest,
    StructuredResponse,
    TokenCountRequest,
    TokenCountResponse,
    TokenRates,
    TokenUsage,
    ToolCall,
)
from .streaming import SSE_HEADERS, SSE_MEDIA_TYPE, sse_from_sync_iterator

logger: logging.Logger = logging.getLogger(__name__)

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


def _registry_entry(model: str) -> Any | None:
    """Find a model's registry entry without knowing its provider.

    The pricing registry keys on the provider vendor ("anthropic"), while
    callers select an engine ("claude"), and the library exposes no public
    engine-to-vendor mapping. Hardcoding one here would drift the first time
    the library adds an engine.

    Instead the registry's own key set is searched for the model name. Model
    names are provider-specific in practice, so a name identifies its entry.

    Args:
        model: Provider-reported model name.

    Returns:
        Any | None: The registry entry, or None when the model is not
            catalogued.
    """
    for provider, catalogued_model in DICT_MODEL_INFO:
        if catalogued_model == model:
            return get_model_info(provider, model)
    return None


async def _embed_batch(client: Any, inputs: list[str]) -> list[dict[str, Any]]:
    """Embed a batch, falling back to the sync call when async is unsupported.

    The batch call is used rather than one call per input: N single calls would
    multiply provider round trips and cost events for one request.

    Not every engine implements the async surface — Gemini's embedding models
    raise `AiProviderCapabilityUnsupportedError` for it, and Bedrock has no
    async at all. The sync call still works, so it runs in the threadpool
    rather than blocking the event loop, which is the same treatment
    `count_tokens` gets.

    Args:
        client: Pooled embeddings client.
        inputs: Texts to embed.

    Returns:
        list[dict[str, Any]]: One provider result per input.
    """
    try:
        return await client.agenerate_embeddings_batch(inputs)
    except AiProviderCapabilityUnsupportedError:
        logger.info(
            "engine has no async embeddings; using the sync call in the threadpool"
        )
        return await run_in_threadpool(client.generate_embeddings_batch, inputs)


# Keys providers use for the vector in an embeddings result.
_VECTOR_KEYS: tuple[str, ...] = ("embedding", "values", "vector")


def _vector_from(result: dict[str, Any]) -> list[float]:
    """Pull the vector out of a provider embeddings result.

    The library returns the provider's own dict, and providers disagree on the
    key. Reading several is what keeps this endpoint working across engines
    without a per-provider branch here.

    Args:
        result: One provider embeddings result.

    Returns:
        list[float]: The vector.

    Raises:
        HTTPException: 502 when no known key is present, since a silently empty
            vector would be worse than a clear failure.
    """
    for key in _VECTOR_KEYS:
        value = result.get(key)
        if isinstance(value, list):
            return [float(component) for component in value]
    raise HTTPException(
        status_code=502,
        detail=(
            f"Embeddings result carried no vector under any known key "
            f"({', '.join(_VECTOR_KEYS)}); keys present: "
            f"{', '.join(sorted(result)) or 'none'}."
        ),
    )


def _model_info(info: Any) -> ModelInfo:
    """Convert a registry entry into the response model."""
    return ModelInfo(
        provider=info.provider,
        model=info.model,
        status=info.status.value,
        sunset_date=info.sunset_date.isoformat() if info.sunset_date else None,
        recommended_replacement=info.recommended_replacement,
        pricing=_model_pricing(info.pricing),
    )


def _model_pricing(pricing: Any) -> ModelPricing | None:
    """Convert registry pricing, keeping money values as strings.

    Decimal money cannot round-trip through binary floating point, so rates
    are serialized as strings rather than becoming 0.07499999999999999 in a
    field callers may use to compute cost.
    """
    if pricing is None:
        return None
    rates = pricing.token_rates
    return ModelPricing(
        unit=pricing.unit.value,
        currency=pricing.currency,
        effective_date=pricing.effective_date.isoformat(),
        source=pricing.source,
        confidence=pricing.confidence,
        token_rates=(
            TokenRates(
                input_per_1m=str(rates.input_per_1m),
                output_per_1m=(
                    str(rates.output_per_1m)
                    if rates.output_per_1m is not None
                    else None
                ),
                cached_input_per_1m=(
                    str(rates.cached_input_per_1m)
                    if rates.cached_input_per_1m is not None
                    else None
                ),
            )
            if rates is not None
            else None
        ),
        notes=pricing.notes,
    )


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
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Embedding vectors for one or more inputs",
)
async def embeddings(request: EmbeddingsRequest) -> EmbeddingsResponse:
    """Embed one or more inputs.

    Args:
        request: Engine selection and the texts to embed.

    Returns:
        EmbeddingsResponse: One vector per input, index-paired to the inputs.
    """
    if not request.inputs:
        raise HTTPException(status_code=400, detail="inputs must not be empty.")

    client = get_embeddings_client(request.engine, request.model)
    results: list[dict[str, Any]] = await _embed_batch(client, request.inputs)

    vectors: list[EmbeddingVector] = [
        EmbeddingVector(index=index, embedding=_vector_from(result))
        for index, result in enumerate(results)
    ]
    return EmbeddingsResponse(
        vectors=vectors,
        engine=request.engine,
        model=request.model,
        dimensions=len(vectors[0].embedding) if vectors else None,
    )


@router.post(
    "/tokens/count",
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Provider-side token count for a prompt",
)
async def token_count(request: TokenCountRequest) -> TokenCountResponse:
    """Count tokens in a prompt, as the provider counts them.

    `count_tokens` is synchronous and some providers make a network call for
    it, so it runs in the threadpool rather than blocking the event loop.

    Args:
        request: Engine selection and the prompt to measure.

    Returns:
        TokenCountResponse: The provider-side token count.
    """
    client = get_completions_client(request.engine, request.model)
    count: int = await run_in_threadpool(client.count_tokens, request.prompt)
    return TokenCountResponse(
        token_count=count, engine=request.engine, model=request.model
    )


@router.get(
    "/models",
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Catalogued models with capabilities, pricing, and lifecycle",
)
async def models(engine: str) -> ModelsResponse:
    """List an engine's models with their lifecycle and pricing.

    `engine` is required rather than optional. Listing every engine would mean
    constructing a client for each one on a single request, and client
    construction re-reads configuration and makes a network round trip on
    Gemini. One engine per call keeps the cost of this endpoint predictable.

    Args:
        engine: Engine token to list.

    Returns:
        ModelsResponse: Provider-reported models, plus registry entries for
            lifecycle and pricing.
    """
    client = get_completions_client(engine, None)
    # list_completion_models talks to the provider, so it goes to the threadpool.
    names: list[str] = await run_in_threadpool(AIFactory.list_completion_models, client)

    catalog: list[ModelInfo] = []
    for name in names:
        info = _registry_entry(name)
        if info is not None:
            catalog.append(_model_info(info))
    return ModelsResponse(engine=engine, models=names, catalog=catalog)
