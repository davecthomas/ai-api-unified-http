# src/ai_api_unified_http/routes_v1.py

"""
v1 API routes.

Every model-invoking endpoint returns HTTP 501 until its implementation
lands; the request schemas are real so the OpenAPI spec and generated
TypeScript client are stable from day one. The endpoint-to-library-call
mapping lives in docs/technical-design.md.
"""

from collections.abc import Iterator

from ai_api_unified import AICompletionsPromptParamsBase
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .clients import get_completions_client
from .schemas import (
    CompletionRequest,
    CompletionResponse,
    ConversationTurnRequest,
    EmbeddingsRequest,
    ErrorResponse,
    NotImplementedResponse,
    StructuredRequest,
    TokenCountRequest,
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
    responses={501: {"model": NotImplementedResponse}},
    summary="Schema-validated structured output",
)
def structured(request: StructuredRequest) -> JSONResponse:
    """Will map to asend_structured_output."""
    return _not_implemented("/v1/structured")


@router.post(
    "/conversations/turn",
    responses={501: {"model": NotImplementedResponse}},
    summary="One stateless conversation turn, with optional tool schemas",
)
def conversation_turn(request: ConversationTurnRequest) -> JSONResponse:
    """Will map to asend_conversation; the caller owns the tool loop."""
    return _not_implemented("/v1/conversations/turn")


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
