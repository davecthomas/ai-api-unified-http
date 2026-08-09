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


class TokenUsage(BaseModel):
    """Token counts for one call, as the provider reported them.

    Every field is optional because not every provider reports every count,
    and the library passes through what it was given rather than inventing
    zeros that would read as measured values.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    total_tokens: int | None = None


class StructuredResponse(BaseModel):
    """Schema-validated structured output.

    `data` is null whenever `finish_reason` is not `complete`: the model was
    cut off by the token budget or refused. Read `finish_reason` before `data`,
    or a truncated response is indistinguishable from an empty result.
    """

    data: dict[str, Any] | None = Field(
        default=None,
        description="Parsed object, or null when finish_reason is not 'complete'.",
    )
    finish_reason: str = Field(
        description="One of: complete, length, tool_use, refusal."
    )
    usage: TokenUsage
    raw_text: str = Field(
        description="Model output before parsing, for diagnosing a null data field."
    )
    engine: str
    model: str | None = None


class ToolCall(BaseModel):
    """A tool the model wants invoked.

    The service never executes these. The caller runs the tool and sends the
    result back on the next turn, which is what keeps the service stateless
    and keeps tool execution inside the caller's own trust boundary.
    """

    id: str = Field(description="Echo this back when returning the tool result.")
    name: str
    input: dict[str, Any]


class ConversationTurnResponse(BaseModel):
    """One assistant turn.

    `conversation_token` is opaque. Store it and echo it back on the next turn
    without inspecting it: it carries provider-specific content whose shape
    changes with the engine and the library version, and parsing it would turn
    a provider's internal representation into this service's contract.
    """

    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = Field(
        description="One of: complete, length, tool_use, refusal."
    )
    usage: TokenUsage
    conversation_token: str | None = Field(
        default=None,
        description=(
            "Opaque. Echo back verbatim as previous_turn_token on the next "
            "turn. Never parse it; the encoding is an implementation detail "
            "that changes between service versions."
        ),
    )
    engine: str
    model: str | None = None


class ToolSchema(BaseModel):
    """Mirror of ai_api_unified.AITool — a schema, never executable code."""

    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool = False


class ConversationTurnRequest(EngineSelection):
    system_prompt: str
    messages: list[dict[str, Any]] = Field(
        description=(
            "Full conversation history; the service holds no state between "
            "turns. Replay a previous assistant turn by placing its "
            "conversation_token as the content of an assistant message, in "
            "the position it occurred: "
            '{"role": "assistant", "content": "<conversation_token>"}. '
            "Ordering is yours, because only you know where a new user "
            "message belongs relative to it."
        )
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


class EmbeddingVector(BaseModel):
    """One input's embedding, paired with the index of the input it came from.

    The index is explicit rather than positional-by-convention so a caller can
    reorder or filter without losing the mapping back to their inputs.
    """

    index: int
    embedding: list[float]


class EmbeddingsResponse(BaseModel):
    vectors: list[EmbeddingVector]
    engine: str
    model: str | None = None
    dimensions: int | None = Field(
        default=None, description="Length of each vector, when any were returned."
    )


class TokenCountResponse(BaseModel):
    token_count: int = Field(description="Provider-side count for the prompt.")
    engine: str
    model: str | None = None


class TokenRates(BaseModel):
    """Per-million-token prices.

    Serialized as strings, not floats. These are decimal money values, and
    binary floating point cannot represent them exactly — a rate of 0.075
    becoming 0.07499999999999999 in JSON would be wrong in a field callers may
    use to compute or display cost.
    """

    input_per_1m: str
    output_per_1m: str | None = None
    cached_input_per_1m: str | None = None


class ModelPricing(BaseModel):
    unit: str
    currency: str
    effective_date: str
    source: str
    confidence: str
    token_rates: TokenRates | None = None
    notes: str | None = None


class ModelInfo(BaseModel):
    """Catalog entry for one model.

    `status` and `sunset_date` are the fields worth reading before pinning a
    model: the library refuses retired models outright, and a deprecated one
    names its replacement.
    """

    provider: str
    model: str
    status: str = Field(description="One of: active, deprecated, retired.")
    sunset_date: str | None = None
    recommended_replacement: str | None = None
    pricing: ModelPricing | None = None


class ModelsResponse(BaseModel):
    """Model catalog for one engine.

    `models` lists what the provider reports as available right now.
    `catalog` carries the library's registry entries — lifecycle and pricing —
    which cover models the provider may not list and omit models the registry
    has not catalogued. The two are reported separately rather than merged,
    because a model missing from one is a different fact than a model missing
    from the other.
    """

    engine: str
    models: list[str]
    catalog: list[ModelInfo]


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
