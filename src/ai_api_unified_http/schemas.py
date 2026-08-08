# src/ai_api_unified_http/schemas.py

"""
Request and response schemas for the v1 API.

These models exist from the first commit so the OpenAPI spec — and the
TypeScript client generated from it — carries real request shapes even
while endpoints still return 501. Field names mirror the ai-api-unified
call signatures they will map onto (see docs/technical-design.md).
"""

from typing import Any

from pydantic import BaseModel, Field


class EngineSelection(BaseModel):
    """Common provider-selection fields accepted by every model-invoking route."""

    engine: str = Field(
        description="Completions engine token, e.g. 'openai', 'claude', 'google-gemini'."
    )
    model: str | None = Field(
        default=None,
        description="Model name; omit to use the engine's default model.",
    )


class CompletionRequest(EngineSelection):
    prompt: str
    system_prompt: str | None = None
    max_response_tokens: int | None = None
    request_timeout_seconds: float | None = None
    stream: bool = Field(
        default=False,
        description="When true the response is text/event-stream (SSE). "
        "Streaming responses bypass PII redaction middleware by library design.",
    )


class StructuredRequest(EngineSelection):
    prompt: str | None = None
    messages: list[dict[str, Any]] | None = None
    system_prompt: str | None = None
    response_schema: dict[str, Any] = Field(
        description="JSON schema the completion must validate against."
    )
    max_response_tokens: int | None = None
    request_timeout_seconds: float | None = None


class ToolSchema(BaseModel):
    """Mirror of ai_api_unified.AITool — a schema, never executable code."""

    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool = False


class ConversationTurnRequest(EngineSelection):
    system_prompt: str
    messages: list[dict[str, Any]] = Field(
        description="Full conversation history; the service holds no state between turns."
    )
    tools: list[ToolSchema] | None = None
    tool_choice: str | None = None
    max_response_tokens: int | None = None
    request_timeout_seconds: float | None = None


class EmbeddingsRequest(BaseModel):
    engine: str = Field(
        description="Embeddings engine token, e.g. 'openai', 'google-gemini', 'voyage'."
    )
    model: str | None = None
    inputs: list[str] = Field(description="Texts to embed; one vector per input.")


class TokenCountRequest(EngineSelection):
    prompt: str


class NotImplementedResponse(BaseModel):
    """Body returned by every endpoint that is scaffolded but not yet live."""

    error: str = "not_implemented"
    endpoint: str
    detail: str = "Planned for a future release; see docs/requirements.md."


class HealthResponse(BaseModel):
    status: str
    service_version: str
    api_version: str
    library_version: str
