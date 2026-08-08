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

Implemented in `clients.py`. Two properties below are deliberate choices:

- **`model=None` is its own pool entry**, distinct from any named model, even
  when the engine default resolves to that same model. The pool keys on the
  caller's request, before the engine resolves a default.
- **Construction runs outside the lock.** Two requests racing on the same cold
  key can both build a client, and the loser's copy is discarded. Holding the
  lock across construction would serialize every cold start behind one Gemini
  network round trip, and a duplicate build is harmless because the clients
  carry no state worth preserving.

A construction failure leaves the key cold, so a request that arrives after
the misconfiguration is fixed succeeds without a restart.

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

Implemented in `cost.py`, attached and verified by the app lifespan.

| Variable | Default | Purpose |
|---|---|---|
| `HTTP_COST_LOG_PATH` | `cost-events.jsonl` | Sink file; parent directories are created |
| `HTTP_COST_TOPIC` | `ai_api_unified.observability.cost` | Logger to attach to; set it when the library's `emit_cost_topic` is retuned |

Startup attaches the handler, then calls `verify_cost_capture()`, which raises
`CostEventNotCapturedError` when the topic has no handler. The check also
catches a deployment that retuned the library's `emit_cost_topic` without
telling the service, where capture would attach to a topic nothing publishes
to and every event would vanish. There is no opt-out, because hard
requirement 3 admits none.

The handler holds three properties deliberately:

- **Unknown fields pass through.** Every non-standard attribute on the log
  record is carried into the JSON object; there is no allowlist to update, so
  a field the library adds later survives with no change here.
- **The topic records at `INFO` regardless of the root logger.** A deployment
  running the root at `WARNING` must not thereby drop every cost event.
- **A failing sink never raises into the request.** Losing one event is bad;
  taking down the call that produced it is worse, so write errors route
  through the logging error path.

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

## Error mapping

Implemented in `errors.py`. The governing question is whose fault the failure
is: a caller who can fix the request gets a 4xx, and anything they cannot act
on becomes a 5xx. Provider credentials live only in the service environment,
so a provider auth rejection is a service misconfiguration reported with the
provider's status code, and returning it verbatim would tell the caller to fix
a key they do not hold.

`AiProviderRequestError` carries the provider's own `status_code` and
`provider_engine`, so classification reads them directly instead of matching
message text.

| Library exception | Condition | HTTP status | `error` code |
|---|---|---|---|
| `AiProviderRequestError` | provider 429 | 429 | `provider_rate_limited` |
| `AiProviderRequestError` | provider 401 / 403 | 502 | `provider_auth_failed` |
| `AiProviderRequestError` | provider 5xx | 502 | `provider_error` |
| `AiProviderRequestError` | other provider 4xx | 400 | `provider_rejected_request` |
| `AiProviderRequestError` | no status (connection error, client timeout) | 502 | `provider_unavailable` |
| `AiProviderConfigurationError` | retired model, bad config | 400 | `invalid_request` |
| `AiProviderCapabilityUnsupportedError` | engine cannot do it | 400 | `invalid_request` |
| `AiProviderDependencyUnavailableError` | provider SDK not installed | 503 | `provider_dependency_unavailable` |
| `StructuredResponseTokenLimitError` | response truncated by budget | 422 | `structured_response_token_limit` |
| `AiProviderError` | anything else in the hierarchy | 502 | `provider_error` |
| Request validation | malformed body | 422 | FastAPI default |

Every mapped failure returns the same body: `error`, `detail`, `engine`, and
`provider_status`. `provider_status` is the provider's status when one was
reported, and never equals the response status — a provider 500 surfaces as a
502 here.

- **`Retry-After` is not forwarded.** The bootstrap plan called for passing it
  through on a 429, but `AiProviderRequestError` carries only the status code,
  not the provider's response headers, so the service has no value to forward.
  Callers back off on their own schedule.
- **The token-limit 422 names the fix.** `StructuredResponseTokenLimitError`
  reports `minimum_supported_tokens` for the model, so the detail states the
  floor rather than leaving the caller to bisect `max_response_tokens`.

Handler registration order matters: every one of these inherits
`AiProviderError`, so the base is registered last. Registering it first would
collapse the whole table into one 502.

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
