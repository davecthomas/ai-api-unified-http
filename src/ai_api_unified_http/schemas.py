# src/ai_api_unified_http/schemas.py

"""
Request and response schemas for the v1 API.

These models are the published contract: the OpenAPI document is generated
from them, and the TypeScript client from that. Field names mirror the
ai-api-unified call signatures they map onto (see docs/technical-design.md).
"""

from typing import Any, Final

from pydantic import BaseModel, Field

# Ceilings on caller-supplied collections and text.
#
# These are part of the API contract rather than deployment configuration, and
# deliberately so: they appear in the OpenAPI document, which is published and
# is what the TypeScript client is generated from. A limit that varied per
# deployment would make the spec, and therefore the generated client,
# deployment-specific. `HTTP_MAX_REQUEST_BYTES` is the tunable ceiling; these
# bound the shape.
MAX_PROMPT_CHARS: Final[int] = 200_000
MAX_SYSTEM_PROMPT_CHARS: Final[int] = 50_000
MAX_EMBEDDING_INPUTS: Final[int] = 256
MAX_CONVERSATION_MESSAGES: Final[int] = 500

# Batch submissions carry many prompts in one body, so this interacts with
# HTTP_MAX_REQUEST_BYTES: a batch near this count will exceed the 1 MiB default
# and be refused with 413 long before the count is reached. Raise that setting
# on a deployment that submits large batches.
MAX_BATCH_REQUESTS: Final[int] = 1_000

# Each image is generated, fetched, and written to the artifact store before
# the response returns, so this bounds how long one request can take as much as
# how much it can cost.
MAX_IMAGES_PER_REQUEST: Final[int] = 4

# Attachments on a completion. The library caps the decoded bytes itself, at 20
# MB per item and in total, so this bounds only how many the service will
# decode before handing them over.
MAX_ATTACHMENTS: Final[int] = 8


class EngineSelection(BaseModel):
    """Common provider-selection fields accepted by every model-invoking route."""

    engine: str = Field(
        description="Completions engine token, e.g. 'openai', 'claude', 'google-gemini'."
    )
    model: str | None = Field(
        default=None,
        description="Model name; omit to use the engine's default model.",
    )


class Attachment(BaseModel):
    """One non-text input attached to a prompt.

    The bytes come from one of two places and exactly one must be given.
    `data` carries them base64-encoded, which is what a JSON body allows.
    `artifact_id` names something already in the artifact store, so an image
    this service generated can be asked about without being downloaded and
    uploaded again.

    Which attachment types a model accepts is the library's decision, not this
    service's: it validates the MIME type against what the provider path
    supports and refuses the rest. Today that is images. A request carrying
    anything else comes back as a 400 quoting the library's own reason, and
    widening upstream widens this endpoint without a change here.
    """

    mime_type: str | None = Field(
        default=None,
        description=(
            "Content type of the attached bytes, required with `data`. Taken "
            "from the stored artifact when `artifact_id` is used instead."
        ),
    )
    data: str | None = Field(
        default=None,
        description="Base64-encoded bytes. Mutually exclusive with artifact_id.",
    )
    artifact_id: str | None = Field(
        default=None,
        description=(
            "An artifact previously returned by /v1/images or /v1/videos, read "
            "from the store instead of being re-uploaded. Only the caller who "
            "created it can attach it."
        ),
    )


class CompletionRequest(EngineSelection):
    prompt: str = Field(max_length=MAX_PROMPT_CHARS)
    attachments: list[Attachment] | None = Field(
        default=None,
        max_length=MAX_ATTACHMENTS,
        description=(
            "Non-text inputs to send with the prompt. Works on the streaming "
            "path as well as the buffered one. Base64 inflates by a third, so "
            "an attachment sent as `data` counts against "
            "HTTP_MAX_REQUEST_BYTES (1 MiB by default) and will be refused "
            "with 413 well before the library's own 20 MB cap; an "
            "`artifact_id` carries no body weight at all."
        ),
    )
    system_prompt: str | None = Field(default=None, max_length=MAX_SYSTEM_PROMPT_CHARS)
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
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    messages: list[dict[str, Any]] | None = Field(
        default=None, max_length=MAX_CONVERSATION_MESSAGES
    )
    system_prompt: str | None = Field(default=None, max_length=MAX_SYSTEM_PROMPT_CHARS)
    response_schema: dict[str, Any] = Field(
        description="JSON schema the completion must validate against."
    )
    max_response_tokens: int | None = None
    request_timeout_seconds: float | None = None


USD_COST_DESCRIPTION: Final[str] = (
    "Cost of this call in USD, priced from the usage above, or null when the "
    "model carries no token rates in the registry. A string for the same "
    "reason the rates are strings: these are decimal money values, and binary "
    "floating point cannot hold them exactly. Null is not zero — null means "
    "the price is unknown, and a call that genuinely cost nothing reports a "
    "numeric zero."
)


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
    usd_cost: str | None = Field(default=None, description=USD_COST_DESCRIPTION)
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
    usd_cost: str | None = Field(default=None, description=USD_COST_DESCRIPTION)
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
    system_prompt: str = Field(max_length=MAX_SYSTEM_PROMPT_CHARS)
    messages: list[dict[str, Any]] = Field(
        max_length=MAX_CONVERSATION_MESSAGES,
        description=(
            "Full conversation history; the service holds no state between "
            "turns. Replay a previous assistant turn by placing its "
            "conversation_token as the content of an assistant message, in "
            "the position it occurred: "
            '{"role": "assistant", "content": "<conversation_token>"}. '
            "Ordering is yours, because only you know where a new user "
            "message belongs relative to it."
        ),
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
    inputs: list[str] = Field(
        max_length=MAX_EMBEDDING_INPUTS,
        description="Texts to embed; one vector per input.",
    )
    input_type: str | None = Field(
        default=None,
        description=(
            "What the text is for, typically 'query' or 'document'. Voyage and "
            "Gemini embed a search query differently from a stored document, "
            "so a retrieval index built without this and searched with it "
            "returns worse matches. Forwarded to the provider unchanged; "
            "engines that do not use it ignore it."
        ),
    )


class TokenCountRequest(EngineSelection):
    prompt: str = Field(max_length=MAX_PROMPT_CHARS)


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


class BatchRequestItem(BaseModel):
    """One prompt in a batch, carrying the caller's own identifier.

    `custom_id` is how a result is matched back to its request. Providers
    return results in their own order, so position cannot be relied on.
    """

    custom_id: str = Field(
        max_length=128,
        description="Caller's identifier for this item; must be unique in the batch.",
    )
    prompt: str = Field(max_length=MAX_PROMPT_CHARS)
    system_prompt: str | None = Field(default=None, max_length=MAX_SYSTEM_PROMPT_CHARS)
    max_response_tokens: int | None = None


class BatchSubmitRequest(EngineSelection):
    """A batch of prompts submitted as one job.

    Batch pricing is roughly half of interactive at the providers that offer
    it, in exchange for latency measured in hours rather than seconds.
    """

    requests: list[BatchRequestItem] = Field(
        max_length=MAX_BATCH_REQUESTS,
        description="Prompts to run; each needs a custom_id unique within the batch.",
    )


class BatchJobResponse(BaseModel):
    """A batch's identity and current state.

    `batch_id` has to be presented with the same `engine` and `model` on every
    later call. A batch lives inside one provider's account, and the client
    that can ask about it is the one built for that engine, so the id alone
    does not identify it.

    Counts are optional because providers report different subsets, and the
    library passes through what it was given rather than inventing zeros that
    would read as measured values.
    """

    batch_id: str
    provider_batch_id: str | None = None
    status: str = Field(
        description=(
            "One of: in_progress, canceling, ended, failed, expired, canceled. "
            "Results are available once the status is ended."
        )
    )
    request_count: int | None = None
    succeeded_count: int | None = None
    errored_count: int | None = None
    canceled_count: int | None = None
    expired_count: int | None = None
    processing_count: int | None = None
    submitted_at_utc: str | None = Field(
        default=None, description="ISO 8601 UTC timestamp, or null."
    )
    ended_at_utc: str | None = Field(
        default=None, description="ISO 8601 UTC timestamp, or null."
    )
    engine: str
    model: str | None = None


class BatchResultItem(BaseModel):
    """One request's outcome.

    An item can fail while the batch as a whole ends normally, so `status` is
    per item and `text` is null unless it succeeded.
    """

    custom_id: str
    status: str = Field(description="One of: succeeded, errored, canceled, expired.")
    text: str | None = None
    error_message: str | None = None
    usage: TokenUsage


class BatchResultsResponse(BaseModel):
    """Every result for an ended batch.

    Correlate by `custom_id`. Provider order is not request order.
    """

    batch_id: str
    results: list[BatchResultItem]
    engine: str
    model: str | None = None


class ImageRequest(BaseModel):
    """A prompt and the shape of the images wanted from it."""

    prompt: str = Field(max_length=MAX_PROMPT_CHARS)
    model: str | None = Field(
        default=None, description="Image model, or null for the configured default."
    )
    width: int | None = Field(default=None, ge=64, le=4096)
    height: int | None = Field(default=None, ge=64, le=4096)
    image_format: str = Field(default="png", description="One of: png, jpeg, webp.")
    quality: str = Field(default="medium", description="Provider quality hint.")
    background: str = Field(default="auto", description="Provider background hint.")
    num_images: int = Field(
        default=1,
        ge=1,
        le=MAX_IMAGES_PER_REQUEST,
        description=(
            "How many images to generate. Capped because each one is fetched "
            "and stored before the response returns."
        ),
    )


class ArtifactRef(BaseModel):
    """Where to fetch one generated artifact, and how big it is.

    The bytes are not inlined. They are fetched from `/v1/artifacts/{id}/content`,
    which sends `Content-Length` so a client can draw a real progress bar, and
    honours `Range` so a failed transfer resumes instead of restarting.
    """

    artifact_id: str
    mime_type: str
    size_bytes: int = Field(
        description="Exact byte count, matching the Content-Length of a full fetch."
    )
    kind: str = Field(description="One of: image, video.")
    url_path: str = Field(
        description="Path to fetch the bytes from, relative to the service root."
    )


class ImageResponse(BaseModel):
    """What a generation produced, without the bytes."""

    artifacts: list[ArtifactRef]
    engine: str | None = None
    model: str | None = None


class VideoRequest(BaseModel):
    """A prompt and the shape of the video wanted from it."""

    prompt: str = Field(max_length=MAX_PROMPT_CHARS)
    engine: str | None = Field(
        default=None, description="Video engine, or null for the configured default."
    )
    model: str | None = None
    duration_seconds: int | None = Field(default=None, ge=1, le=60)
    aspect_ratio: str | None = None
    resolution: str | None = None
    fps: int | None = Field(default=None, ge=1, le=120)
    seed: int | None = None
    output_format: str = Field(default="mp4")


class JobResponse(BaseModel):
    """A generation job's progress.

    `percent` is a number a UI can render directly. `estimated` says whether it
    was measured or derived: providers do not all report progress, so an
    estimate from elapsed time is offered rather than nothing, and is labelled
    so a caller can present it honestly. An estimate never reaches 100 — only
    the job finishing sets that.
    """

    job_id: str
    status: str = Field(description="One of: queued, generating, ready, failed.")
    percent: float = Field(description="Completion in [0, 100].")
    estimated: bool = Field(
        description="True when percent is derived from elapsed time, not reported."
    )
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    error: str | None = None
    engine: str | None = None
    model: str | None = None


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
