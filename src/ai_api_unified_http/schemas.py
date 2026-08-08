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


class CompletionResponse(BaseModel):
    """Buffered completion result.

    Text only, because `asend_prompt` returns a bare `str`. Token usage and a
    finish reason would require routing this through the conversation call
    instead, which is a different provider path than the endpoint documents.
    `/v1/conversations/turn` and `/v1/structured` return both natively.
    """

    text: str = Field(description="Generated completion text.")
    engine: str = Field(description="Engine that served the request.")
    model: str | None = Field(
        default=None, description="Model requested, or null for the engine default."
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


class ErrorResponse(BaseModel):
    """Uniform failure body for every error the service maps from the library.

    `provider_status` is the provider's own HTTP status when one was reported.
    It is present for diagnosis and never equals the status of this response:
    a provider 500 surfaces as a 502 here, and a provider 401 as a 502 too,
    since callers hold no provider credentials.
    """

    error: str = Field(description="Machine-readable error code.")
    detail: str = Field(description="Human-readable cause.")
    engine: str | None = Field(
        default=None, description="Engine token that failed, when known."
    )
    provider_status: int | None = Field(
        default=None,
        description="HTTP status reported by the provider, when one was reported.",
    )


class HealthResponse(BaseModel):
    status: str
    service_version: str
    api_version: str
    library_version: str
