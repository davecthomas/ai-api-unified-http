# src/ai_api_unified_http/routes_v1.py

"""
v1 API routes.

Each endpoint adapts one library call: it selects a pooled client, translates
the request, and shapes the result. Provider behaviour, retries, and pricing
belong to the library. The endpoint-to-library-call mapping lives in
docs/technical-design.md.
"""

import asyncio
import logging
from collections import Counter
from collections.abc import Iterator
from decimal import Decimal
from functools import partial
from typing import Any

from ai_api_unified import (
    AIBaseImageProperties,
    AIBaseVideoProperties,
    AIBatchRequestItem,
    AICompletionsPromptParamsBase,
    AIFactory,
    AiProviderCapabilityUnsupportedError,
    AITool,
    get_model_info,
)
from ai_api_unified.pricing import DICT_MODEL_INFO
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from .artifacts import (
    ArtifactNotFoundError,
    ArtifactRecord,
    JobRecord,
    new_id,
    read_job,
    read_record,
    store_artifact,
    write_job,
)
from .clients import (
    get_completions_client,
    get_embeddings_client,
    get_images_client,
    get_video_client,
)
from .conversation_token import (
    InvalidConversationTokenError,
    decode_conversation_token,
    encode_conversation_token,
    looks_like_conversation_token,
)
from .delivery import artifact_response, not_found
from .jobs import run_video_job
from .schemas import (
    ArtifactRef,
    BatchJobResponse,
    BatchResultItem,
    BatchResultsResponse,
    BatchSubmitRequest,
    CompletionRequest,
    CompletionResponse,
    ConversationTurnRequest,
    ConversationTurnResponse,
    EmbeddingsRequest,
    EmbeddingsResponse,
    EmbeddingVector,
    ErrorResponse,
    ImageRequest,
    ImageResponse,
    JobResponse,
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
    VideoRequest,
)
from .streaming import (
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    sse_from_sync_iterator,
    sse_job_progress,
)

logger: logging.Logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/v1")

_NOT_IMPLEMENTED_STATUS: int = 501

# Requested image format to the content type the artifact is served as.
_IMAGE_MIME_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
}

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


def _usd_cost(client: Any, usage: Any) -> str | None:
    """Price measured usage, or return None when the model has no rates.

    The library's `compute_completion_cost` answers 0.0 for a model with no
    pricing on record, which a caller cannot tell from a call that genuinely
    cost nothing. The rates are read directly instead, so an unpriced model
    reports null and a priced one reports a figure that means what it says.

    `compute_token_cost` treats cached input tokens as a subset of the input
    count, so the non-cached remainder is what it is given.

    A failure to price never fails the request. The provider call has already
    happened and already been billed by the time this runs, so raising here
    would take away a result the caller has paid for in exchange for a number
    they can compute themselves from `/v1/models`. An unpriceable call reports
    the same null as an unpriced model.

    Args:
        client: The pooled client that served the call, carrying the rates.
        usage: The library's token usage for the call.

    Returns:
        str | None: The exact decimal cost as a string, or None when the model
            carries no token rates or the rates could not be applied.
    """
    pricing: Any = getattr(getattr(client, "capabilities", None), "pricing", None)
    if pricing is None or getattr(pricing, "token_rates", None) is None:
        return None

    cached: int = usage.cached_input_tokens or 0
    non_cached: int = max((usage.input_tokens or 0) - cached, 0)
    try:
        cost: Decimal = pricing.compute_token_cost(
            input_tokens=non_cached,
            output_tokens=usage.output_tokens or 0,
            cached_input_tokens=cached,
        )
    except (ArithmeticError, TypeError, ValueError):
        logger.warning("could not price a completed call; reporting no cost")
        return None
    return str(cost)


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


async def _embed_batch(
    client: Any, inputs: list[str], input_type: str | None = None
) -> list[dict[str, Any]]:
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
        input_type: What the text is for, forwarded unchanged. Engines that do
            not distinguish query from document ignore it.

    Returns:
        list[dict[str, Any]]: One provider result per input.
    """
    try:
        return await client.agenerate_embeddings_batch(inputs, input_type=input_type)
    except AiProviderCapabilityUnsupportedError:
        logger.info(
            "engine has no async embeddings; using the sync call in the threadpool"
        )
        return await run_in_threadpool(
            partial(client.generate_embeddings_batch, inputs, input_type=input_type)
        )


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
        usd_cost=_usd_cost(client, result.usage),
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
        usd_cost=_usd_cost(client, turn.usage),
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
    results: list[dict[str, Any]] = await _embed_batch(
        client, request.inputs, request.input_type
    )

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


# --- Batch ------------------------------------------------------------------
#
# Batch trades latency for price: providers that offer it charge roughly half
# the interactive rate and return results in hours rather than seconds.
#
# `run_batch` is deliberately not exposed. It submits, polls, and blocks until
# the batch ends, which for an HTTP endpoint means holding a connection open
# for hours and losing everything if it drops. Submit, poll, and fetch are
# separate calls here, and the caller keeps the batch id — the same reason
# conversations are stateless.
#
# Every batch call needs a constructed client, and a batch lives inside one
# provider's account, so `engine` travels with the id on every request. The id
# alone does not say which provider holds it.


def _batch_job(job: Any, engine: str, model: str | None) -> BatchJobResponse:
    """Convert the library's job handle into the response model."""
    return BatchJobResponse(
        batch_id=job.batch_id,
        provider_batch_id=job.provider_batch_id,
        status=job.status.value,
        request_count=job.request_count,
        succeeded_count=job.succeeded_count,
        errored_count=job.errored_count,
        canceled_count=job.canceled_count,
        expired_count=job.expired_count,
        processing_count=job.processing_count,
        submitted_at_utc=(
            job.submitted_at_utc.isoformat() if job.submitted_at_utc else None
        ),
        ended_at_utc=job.ended_at_utc.isoformat() if job.ended_at_utc else None,
        engine=engine,
        model=model,
    )


@router.post(
    "/batches",
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Submit a batch of prompts",
)
async def submit_batch(request: BatchSubmitRequest) -> BatchJobResponse:
    """Submit many prompts as one job and return its handle.

    Args:
        request: Engine selection and the prompts to run.

    Returns:
        BatchJobResponse: The job handle. Poll it with GET /v1/batches/{id}.

    Raises:
        HTTPException: 400 when the batch is empty or custom_ids collide, both
            of which the caller can fix without a provider round trip.
    """
    if not request.requests:
        raise HTTPException(status_code=400, detail="requests must not be empty.")

    counts = Counter(item.custom_id for item in request.requests)
    duplicates: set[str] = {i for i, n in counts.items() if n > 1}
    if duplicates:
        # The library raises ValueError for this, which would surface as a 500.
        # Checking here makes it the 400 it is, and names the offenders.
        raise HTTPException(
            status_code=400,
            detail=(
                f"custom_id must be unique within a batch; repeated: "
                f"{', '.join(sorted(duplicates))}."
            ),
        )

    client = get_completions_client(request.engine, request.model)
    items = [
        AIBatchRequestItem(
            custom_id=item.custom_id,
            prompt=item.prompt,
            system_prompt=item.system_prompt,
            max_response_tokens=item.max_response_tokens,
        )
        for item in request.requests
    ]
    job = await run_in_threadpool(client.submit_batch, items)
    return _batch_job(job, request.engine, request.model)


@router.get(
    "/batches/{batch_id}",
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Status of a submitted batch",
)
async def get_batch(
    batch_id: str, engine: str, model: str | None = None
) -> BatchJobResponse:
    """Report a batch's current status and counts.

    Args:
        batch_id: Handle returned by the submit call.
        engine: Engine that holds the batch. Required, because the id alone
            does not identify which provider account it lives in.
        model: Model the batch was submitted against, when it was not default.

    Returns:
        BatchJobResponse: Refreshed status and counts.
    """
    client = get_completions_client(engine, model)
    job = await run_in_threadpool(client.get_batch, batch_id)
    return _batch_job(job, engine, model)


@router.get(
    "/batches/{batch_id}/results",
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Results of an ended batch",
)
async def get_batch_results(
    batch_id: str, engine: str, model: str | None = None
) -> BatchResultsResponse:
    """Return every result for a batch that has ended.

    Correlate results to requests by `custom_id`; providers return them in
    their own order.

    Args:
        batch_id: Handle returned by the submit call.
        engine: Engine that holds the batch.
        model: Model the batch was submitted against, when it was not default.

    Returns:
        BatchResultsResponse: One entry per request, in provider order.
    """
    client = get_completions_client(engine, model)
    items = await run_in_threadpool(client.get_batch_results, batch_id)
    return BatchResultsResponse(
        batch_id=batch_id,
        results=[
            BatchResultItem(
                custom_id=item.custom_id,
                status=item.status.value,
                text=item.text,
                error_message=item.error_message,
                usage=TokenUsage(
                    input_tokens=item.provider_prompt_tokens,
                    output_tokens=item.provider_completion_tokens,
                ),
            )
            for item in items
        ],
        engine=engine,
        model=model,
    )


@router.post(
    "/batches/{batch_id}/cancel",
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Cancel an in-progress batch",
)
async def cancel_batch(
    batch_id: str, engine: str, model: str | None = None
) -> BatchJobResponse:
    """Ask the provider to cancel a batch.

    Cancellation is a request, not a guarantee: items already processed stay
    processed and stay billed, which is why the response carries the counts.

    Args:
        batch_id: Handle returned by the submit call.
        engine: Engine that holds the batch.
        model: Model the batch was submitted against, when it was not default.

    Returns:
        BatchJobResponse: The job in its canceling or canceled state.
    """
    client = get_completions_client(engine, model)
    job = await run_in_threadpool(client.cancel_batch, batch_id)
    return _batch_job(job, engine, model)


# --- Generated media ---------------------------------------------------------
#
# Images and video both produce bytes too large to sit in a JSON body, so the
# generating call returns a manifest and the bytes are fetched separately from
# /v1/artifacts/{id}/content. That split is what makes a progress bar possible:
# the fetch sends Content-Length, and a client draws its bar from bytes read
# against that. It is also what makes a failed transfer cheap, since the fetch
# honours Range and generation has already been paid for.
#
# Images are generated inline because they take seconds. Video takes minutes,
# which is longer than a request should live, so it becomes a job and progress
# is published rather than measured — there are no bytes yet to measure.


def _caller_of(request: Request) -> str:
    """Return the label artifacts are stored under for this request.

    Falls back to a shared bucket only when authentication is disabled, which
    is local development. In a deployment every request carries a label, so one
    caller's artifacts are never reachable with another caller's key.
    """
    return getattr(request.state, "api_key_label", "anonymous")


def _artifact_ref(record: ArtifactRecord) -> ArtifactRef:
    """Convert a stored artifact into the reference a caller fetches with."""
    return ArtifactRef(
        artifact_id=record.artifact_id,
        mime_type=record.mime_type,
        size_bytes=record.size_bytes,
        kind=record.kind,
        url_path=f"/v1/artifacts/{record.artifact_id}/content",
    )


def _job_response(record: JobRecord, refs: list[ArtifactRef]) -> JobResponse:
    """Convert a stored job record into its response shape."""
    return JobResponse(
        job_id=record.job_id,
        status=record.status,
        percent=record.percent,
        estimated=record.estimated,
        artifacts=refs,
        error=record.error,
        engine=record.engine,
        model=record.model,
    )


@router.post(
    "/images",
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Generate images and store them for streaming",
)
async def images(request: Request, body: ImageRequest) -> ImageResponse:
    """Generate images, store each one, and return references to fetch them.

    The bytes are not inlined. Base64 in a JSON body would inflate them by a
    third and cap the response at what a single buffer can hold, and it would
    give a caller no way to show progress or resume a failed transfer.

    Args:
        request: The incoming request, read for the caller's label.
        body: Prompt and image properties.

    Returns:
        ImageResponse: One reference per generated image.
    """
    client = get_images_client(body.model)
    properties = AIBaseImageProperties(
        width=body.width,
        height=body.height,
        format=body.image_format,
        quality=body.quality,
        background=body.background,
        num_images=body.num_images,
    )
    # Generation blocks and is the slow part of this call, so it runs on a
    # worker thread rather than on the event loop.
    blobs: list[bytes] = await run_in_threadpool(
        client.generate_images, body.prompt, properties
    )

    caller: str = _caller_of(request)
    mime_type: str = _IMAGE_MIME_TYPES.get(
        body.image_format.lower(), "application/octet-stream"
    )
    refs: list[ArtifactRef] = []
    for blob in blobs:
        stored = await run_in_threadpool(
            partial(
                store_artifact,
                caller,
                blob,
                mime_type=mime_type,
                kind="image",
                model=body.model,
            )
        )
        refs.append(_artifact_ref(stored))
    return ImageResponse(artifacts=refs, model=body.model)


@router.post(
    "/videos",
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Start a video generation and return its job handle",
)
async def create_video(request: Request, body: VideoRequest) -> JobResponse:
    """Begin generating a video and return immediately with a job handle.

    Poll `/v1/videos/{job_id}` for status, or subscribe to
    `/v1/videos/{job_id}/events` for progress as it changes.

    Args:
        request: The incoming request, read for the caller's label.
        body: Prompt and video properties.

    Returns:
        JobResponse: The job, queued.
    """
    client = get_video_client(body.engine, body.model)
    properties = AIBaseVideoProperties(
        duration_seconds=body.duration_seconds,
        aspect_ratio=body.aspect_ratio,
        resolution=body.resolution,
        fps=body.fps,
        seed=body.seed,
        output_format=body.output_format,
        download_outputs=True,
    )
    caller: str = _caller_of(request)
    record = JobRecord(
        job_id=new_id(), status="queued", engine=body.engine, model=body.model
    )
    await run_in_threadpool(partial(write_job, caller, record))

    # The generation outlives this request, so it is handed to a worker thread
    # rather than awaited. On Cloud Run this needs CPU allocated outside
    # request processing; see the README.
    asyncio.get_running_loop().run_in_executor(
        None, run_video_job, caller, record, client, body.prompt, properties
    )
    return _job_response(record, [])


@router.get(
    "/videos/{job_id}",
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Video job status and progress",
)
async def video_job(request: Request, job_id: str) -> JobResponse:
    """Report a video job's progress and, once ready, its artifacts.

    Args:
        request: The incoming request, read for the caller's label.
        job_id: Handle returned when the job was created.

    Returns:
        JobResponse: Current status, percent, and any artifacts.
    """
    caller: str = _caller_of(request)
    try:
        record: JobRecord = await run_in_threadpool(read_job, caller, job_id)
    except ArtifactNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _job_response(record, await _refs_for(caller, record))


@router.get(
    "/videos/{job_id}/events",
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Progress events for a video job (SSE)",
)
async def video_job_events(request: Request, job_id: str) -> StreamingResponse:
    """Stream a job's progress as it changes, until it finishes.

    Progress during generation cannot be measured by the client, because no
    bytes exist yet. It is published here instead, and each event says whether
    the figure was reported by the provider or estimated from elapsed time.

    Once the job is ready the caller fetches the artifact, and progress for
    *that* phase needs nothing from the service beyond `Content-Length`.

    Args:
        request: The incoming request, read for the caller's label.
        job_id: Handle returned when the job was created.

    Returns:
        StreamingResponse: `text/event-stream` of progress events.
    """
    caller: str = _caller_of(request)
    try:
        await run_in_threadpool(read_job, caller, job_id)
    except ArtifactNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    return StreamingResponse(
        sse_job_progress(caller, job_id),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


@router.get(
    "/artifacts/{artifact_id}/content",
    response_class=StreamingResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Stream a generated artifact's bytes",
)
async def artifact_content(request: Request, artifact_id: str) -> Response:
    """Stream a stored artifact, with a length and resumable ranges.

    `Content-Length` is always sent, which is the whole basis of a client-side
    progress bar. `Range` is honoured, so a transfer that fails partway resumes
    rather than starting over — generation was already paid for.

    Args:
        request: The incoming request, read for the caller's label and Range.
        artifact_id: The artifact to fetch.

    Returns:
        Response: 200 with the whole artifact, or 206 with the requested range.
    """
    caller: str = _caller_of(request)
    try:
        record: ArtifactRecord = await run_in_threadpool(
            read_record, caller, artifact_id
        )
    except ArtifactNotFoundError as error:
        return not_found(str(error))
    return artifact_response(request, caller, record)


async def _refs_for(caller: str, record: JobRecord) -> list[ArtifactRef]:
    """Resolve a job's artifact ids into references, skipping any now gone."""
    refs: list[ArtifactRef] = []
    for artifact_id in record.artifact_ids:
        try:
            stored = await run_in_threadpool(read_record, caller, artifact_id)
        except ArtifactNotFoundError:
            continue
        refs.append(_artifact_ref(stored))
    return refs
