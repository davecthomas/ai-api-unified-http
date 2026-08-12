# Requirements

## Problem

Web applications that are not written in Python need the functionality of
[ai-api-unified](https://github.com/davecthomas/ai-api-unified): one interface
across AI providers, plus the pricing registry, model lifecycle enforcement,
cost attribution, and PII/observability middleware. Porting the library to
TypeScript would duplicate logic and let the two copies drift.

## Decision

Run the Python library behind an HTTP service (this repo), so one
implementation serves every consumer. TypeScript clients are generated from
this service's OpenAPI spec; hand-written clients are out of scope. Decision
record: 2026-08-04.

## Hard requirements

1. **The library does not change to support this service.** The service pins a
   published PyPI release (`ai-api-unified==2.22.0`) and consumes only its
   public API. Library improvements that would simplify the service
   (async streaming, a pluggable cost sink) are tracked as future library work,
   not prerequisites.
2. **Middleware is service-owned.** One middleware YAML owned by the service
   deployment configures the library's observability and PII redaction for
   every call. Observability with cost emission is on in the shipped profile;
   PII redaction is a per-deployment choice, off by default so the streaming
   endpoint works, since the library will not stream while redaction is on.
3. **Cost events reach a durable destination.** The library emits per-call cost
   events as log records on the `ai_api_unified.observability.cost` logger.
   The service attaches a logging handler to that topic and forwards events to
   a durable, queryable destination.
4. **Provider keys live only in the service environment.** Web apps
   authenticate to the service; they never hold OpenAI/Anthropic/Google
   credentials.
5. **Environment variable names match the library's** (`COMPLETIONS_ENGINE`,
   `COMPLETIONS_MODEL_NAME`, `ANTHROPIC_API_KEY`, ...) so a working library
   `.env` works here unchanged.
6. **Configuration is never committed.** `.env` is gitignored; the committed
   `env_template` documents every variable and matches the README setup steps.

## v1 scope

| Endpoint | Purpose |
|---|---|
| `POST /v1/completions` | Text completion, buffered or SSE streaming |
| `POST /v1/structured` | Schema-validated structured output |
| `POST /v1/conversations/turn` | One stateless, tool-capable turn |
| `POST /v1/embeddings` | Embedding vectors |
| `POST /v1/tokens/count` | Provider-side token count |
| `GET /v1/models` | Model catalog: pricing and lifecycle |
| `POST /v1/batches` | Submit many prompts as one job, at batch pricing |
| `GET /v1/batches/{id}` | Batch status and counts |
| `GET /v1/batches/{id}/results` | Per-request results, once ended |
| `POST /v1/batches/{id}/cancel` | Request cancellation |
| `POST /v1/images` | Generate images, stored for streamed retrieval |
| `POST /v1/videos` | Start a video generation job |
| `GET /v1/videos/{id}` | Job status and progress |
| `GET /v1/videos/{id}/events` | Progress events while generating (SSE) |
| `GET /v1/artifacts/{id}/content` | Stream an artifact, resumably |
| `GET /v1/voices` | Voice catalogue, formats, and engine capabilities |
| `POST /v1/speech` | Text to speech, stored for streamed retrieval |
| `GET /healthz` | Liveness and versions |

Every path under `/v1` requires a bearer token. `/healthz` and the OpenAPI
documents do not.

## Out of scope for v1

- **Speech to text** — the input half of voice. The catalogue reports whether
  an engine supports it; no endpoint offers it yet. It takes audio in, which is
  the attachment shape completions already use.

Voice output shipped in 1.8.0. The earlier note read:

- **Voice** — unexposed rather than blocked. The library carries a full voice
  surface (text to speech, speech to text, a voice catalogue) and constructs it
  through `AIVoiceFactory.create()`, which reads `AI_VOICE_ENGINE` and resolves
  the provider through the same registry the other capabilities use. The
  artifact store already handles audio output. What is missing is the work, not
  a decision: an earlier version of this document deferred voice pending a
  storage decision, which was wrong on both counts.
- **Per-user identity and quotas** — API keys name calling applications, not
  people. Per-user identity, quotas, and per-caller rate limits belong behind
  an identity provider.

## Constraints inherited from the library

- Streaming and PII redaction are mutually exclusive: the library raises when
  both are enabled. A deployment that turns redaction on loses streaming, and
  callers needing redaction use the buffered endpoint.
- Bedrock engines have no async support and no per-call timeout.
- Each active SSE stream occupies one threadpool thread until the library
  gains async streaming; concurrency ceiling = threadpool size x workers.
