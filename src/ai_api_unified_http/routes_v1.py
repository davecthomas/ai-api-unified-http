# src/ai_api_unified_http/routes_v1.py

"""
v1 API routes.

Every model-invoking endpoint returns HTTP 501 until its implementation
lands; the request schemas are real so the OpenAPI spec and generated
TypeScript client are stable from day one. The endpoint-to-library-call
mapping lives in docs/technical-design.md.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .schemas import (
    CompletionRequest,
    ConversationTurnRequest,
    EmbeddingsRequest,
    NotImplementedResponse,
    StructuredRequest,
    TokenCountRequest,
)

router: APIRouter = APIRouter(prefix="/v1")

_NOT_IMPLEMENTED_STATUS: int = 501


def _not_implemented(endpoint: str) -> JSONResponse:
    """Uniform 501 body for scaffolded endpoints."""
    body = NotImplementedResponse(endpoint=endpoint)
    return JSONResponse(status_code=_NOT_IMPLEMENTED_STATUS, content=body.model_dump())


@router.post(
    "/completions",
    responses={501: {"model": NotImplementedResponse}},
    summary="Text completion (buffered or SSE stream)",
)
def completions(request: CompletionRequest) -> JSONResponse:
    """Will map to asend_prompt, or send_prompt_streaming when stream=true."""
    return _not_implemented("/v1/completions")


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
