# Technical design

## Architecture

```
web app (TS)  ──generated client──►  ai-api-unified-http (FastAPI)
                                        │  in-process import
                                        ▼
                                  ai-api-unified (PyPI, pinned)
                                        │  provider SDKs
                                        ▼
                            OpenAI / Anthropic / Google / ...
```

The service is a thin adapter: request validation, client pooling, SSE
bridging, and cost-event capture. All provider logic, pricing, lifecycle
enforcement, and middleware stay in the library.

## Stack

- **FastAPI** — Pydantic-native (the library's types are Pydantic, so route
  schemas can wrap them), generates the OpenAPI spec the TypeScript client is
  built from, and handles the library's mixed sync/async surface: async
  routes await the library's `asend_*` coroutines; sync-only paths run in the
  threadpool.
- **uvicorn** (dev) / gunicorn with uvicorn workers (production).

## Endpoint-to-library mapping

| Endpoint | Library call | Notes |
|---|---|---|
| `POST /v1/completions` | `asend_prompt(...)` | True coroutine; handler awaits it. |
| `POST /v1/completions` + `stream: true` | `send_prompt_streaming(...)` | Sync generator bridged to SSE via `StreamingResponse` in the threadpool. No PII redaction (library rule). No retries on streams (library rule). |
| `POST /v1/structured` | `asend_structured_output(...)` | `data` is null on `length`/`refusal` finish reasons; surface `finish_reason` to the caller. |
| `POST /v1/conversations/turn` | `asend_conversation(...)` | Stateless; caller sends full history each turn and executes tools itself (ADR-0018 in the library repo). |
| `POST /v1/embeddings` | `agenerate_embeddings` / `agenerate_embeddings_batch` | |
| `POST /v1/tokens/count` | `count_tokens(...)` | Sync; threadpool. |
| `GET /v1/models` | `list_model_names` + `capabilities` + pricing registry | Read-only registry data: context windows, rates, lifecycle status, replacements. |
| `GET /healthz` | none | Reports service, API, and pinned library versions. |

## Client pool

`AIFactory.get_*` constructs a new provider client per call, and each
construction re-reads `.env`, re-parses the middleware YAML, and (Gemini only)
makes a `models.get` network round trip. The service therefore keeps a
process-wide dict of engine clients keyed by `(engine, model)`, built on first
use and reused. Verified safe: engine instances hold no per-conversation
state, provider SDK clients are thread-safe, and the library's mutable
instance attributes are idempotent lazy caches.

## Conversation `raw_content` round-trip

`AITurnResult.raw_content` is opaque provider-SDK content the library needs
back verbatim on the next turn. The service serializes it into an opaque
string token in the turn response; clients store and echo it untouched. The
token's encoding is a service implementation detail and may change between
service versions — clients must never parse it.

## Cost-event capture

The library emits one structured log record per call (event type
`ai_api_call_cost`) on the logger named by its `emit_cost_topic` setting
(default `ai_api_unified.observability.cost`). At startup the service
attaches a handler to that topic. Bootstrap ships a JSON-lines file handler;
the destination is expected to become a metrics pipeline. Streaming calls
emit their cost event when the stream ends, including client-abandoned
streams (the library handles `GeneratorExit`).

## API versioning

- **URI major version** (`/v1/`): bumps only on breaking changes to request
  or response shapes. `/v1` and `/v2` may serve side by side during a
  migration window.
- **Service version** (semver, `0.1.0`): moves with ordinary releases,
  reported by `/healthz` and the OpenAPI document. See the README for the
  release process.
- The pinned library version is independent of both and reported by
  `/healthz` so operators can correlate service behavior with library
  releases.

## Error mapping (to implement with the first live endpoint)

| Library exception | HTTP status |
|---|---|
| `AiProviderConfigurationError` (retired model, bad config) | 400 |
| `AiProviderCapabilityUnsupportedError` | 400 |
| Provider auth failures | 502 (service-to-provider, not caller's fault) |
| Provider rate limits | 429 with `Retry-After` when the provider supplies one |
| Provider 5xx / network | 502 |
| Validation errors | 422 (FastAPI default) |

## Risks

| Risk | Mitigation |
|---|---|
| SSE ties one thread per active stream | Size threadpool x workers deliberately; pursue async streaming upstream in the library. |
| Gemini client construction does a network call | Pool clients; construction failures surface at first use per (engine, model), not per request. |
| Double retries (library + service) amplifying latency | The service adds no retry layer; retries stay library/SDK-owned. |
| Cost events lost if the handler is misconfigured | Handler attachment is part of app startup; a startup check fails loudly if the topic has no handler. |
| `raw_content` breaks across library upgrades | Token is versioned alongside the service; conversation turns are short-lived. |

## Future work

- Auth layer (before any non-local deployment).
- TypeScript client generation in CI from `/openapi.json`, published to npm.
- Video/batch job resources + object storage for artifacts.
- Library upstream: async streaming, pluggable cost sink, cached config
  loading. None are blockers.
