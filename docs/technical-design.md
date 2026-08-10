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
| `POST /v1/batches` | `submit_batch(...)` | Sync; threadpool. Empty batches and duplicate `custom_id`s are refused as 400s here — the library raises `ValueError` for both, which would surface as 500s. |
| `GET /v1/batches/{id}` | `get_batch(...)` | Sync; threadpool. `engine` is required on every batch call: a batch lives in one provider's account, so the id alone does not identify it. |
| `GET /v1/batches/{id}/results` | `get_batch_results(...)` | Sync; threadpool. Correlate by `custom_id`; provider order is not request order. Results carry usage but no `usd_cost` — the registry's token rates are interactive rates, and batch bills at the provider's batch rate. |
| `POST /v1/batches/{id}/cancel` | `cancel_batch(...)` | Sync; threadpool. Cancellation is a request; already-processed items stay billed. |
| — | `run_batch(...)` | Deliberately unexposed: it blocks until the batch ends, which over HTTP is a connection held for hours that loses the work if it drops. |
| `POST /v1/images` | `generate_images(...)` | Sync; threadpool. Bytes go to the artifact store and the response carries references, because base64 in JSON inflates by a third, caps at one buffer, and gives a caller no way to show progress or resume. |
| `POST /v1/videos` | `submit_video_generation(...)` | Starts a worker-thread job and returns immediately. Generation outlives the request, so on Cloud Run this needs CPU allocated outside request processing. |
| `GET /v1/videos/{id}` | `get_video_generation_job(...)` via the job record | Progress is read from the shared store, not from process memory: the instance polled is usually not the one working. |
| `GET /v1/videos/{id}/events` | none | Service-owned. Emits progress on change and marks it `estimated` when no provider figure exists, so the feature does not depend on a provider reporting one. |
| `GET /v1/artifacts/{id}/content` | none | Service-owned. `Content-Length` is what a client builds a progress bar from; `Range` is what makes a failed transfer a re-download rather than a re-generation. |
| `GET /healthz` | none | Reports service, API, and pinned library versions. |

## Client pool

`AIFactory.get_*` constructs a new provider client per call, and each
construction re-reads `.env`, re-parses the middleware YAML, and (Gemini only)
makes a `models.get` network round trip. The service therefore keeps a
process-wide dict of engine clients keyed by `(engine, model)`, built on first
use and reused. Reuse is safe because engine instances hold no
per-conversation state, provider SDK clients are thread-safe, and the
library's mutable instance attributes are idempotent lazy caches.

Implemented in `clients.py`.

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

## Authentication

Implemented in `auth.py`. Provider credentials live only in the service
environment, so every accepted request spends money the caller never had to
hold a key for. That inverts the usual risk: an unauthenticated endpoint here
is an open tab.

A shared secret in `Authorization: Bearer <key>`, configured through
`HTTP_API_KEYS` as comma-separated `label:key` entries. Several keys are live
at once so a caller can be rotated or revoked alone, and the label names the
calling application in logs. The label authenticates nothing.

- **Enforced as middleware.** A new route is protected the moment it exists,
  so adding an endpoint cannot quietly expose paid capacity by forgetting a
  decorator. A test asserts every `/v1` path in the OpenAPI spec
  answers 401 without a key.
- **CORS wraps auth.** Starlette runs the last-added middleware outermost, so
  CORS is registered after auth. A 401 carries CORS headers, which is what
  lets a browser caller read the body explaining that the key was missing.
- **`OPTIONS` is never gated.** Browsers do not attach `Authorization` to a
  preflight, so gating it would make every cross-origin call fail before it
  could authenticate.
- **Key comparison uses `hmac.compare_digest`** against every configured key,
  so the time taken does not depend on how far the key matched.

Startup raises `AuthNotConfiguredError` when no keys are set and no explicit
opt-out was given. The failure this prevents is a deployment that never set
keys and therefore serves paid endpoints to anyone who can reach the port. `HTTP_AUTH_DISABLED=1` is the deliberate opt-out and logs a warning
on every start.

This is not a user identity system. Keys name calling applications, and the
service stores no per-key state beyond the label. Per-user identity, quotas,
and per-caller rate limits belong behind a real identity provider.

## Conversation `raw_content` round-trip

`AITurnResult.raw_content` is opaque provider-SDK content the library needs
back verbatim on the next turn. The service serializes it into an opaque
string token in the turn response; clients store and echo it untouched. The
token's encoding is a service implementation detail and may change between
service versions — clients must never parse it.

Implemented in `conversation_token.py` as `v1.<base64(compact JSON)>`.

It is deliberately **neither signed nor encrypted**. The content is the
caller's own conversation replayed back to them, so there is nothing there
they did not already send or receive. Signing would add key management and
rotation for no confidentiality gain, and the service still treats a decoded
token as untrusted input.

The version prefix makes an outdated token fail cleanly: when the encoding
changes, an old token is rejected with a message telling the caller to start a
new conversation, instead of reaching a provider as malformed content.
Decoding happens before the client pool is touched, so a bad token costs no
provider call.

Tokens are decoded **in place**, wherever the caller put them: an assistant
message whose content matches `v<digits>.` is expanded to the provider content
it encodes. The service does not append the previous turn itself, because it
cannot know where a new user message belongs relative to it — appending would
turn `[user, assistant, user]` into `[user, user, assistant]` and reorder the
conversation. Any version-shaped prefix counts as a token attempt, so a token
from a retired version is rejected with the same clear message.

## Middleware profile

The library reads one process-wide middleware YAML from
`AI_MIDDLEWARE_CONFIG_PATH`. The service ships `config/middleware.yaml` and
defaults the variable to it when unset, so a stock deployment starts with
observability and cost emission already on.

PII redaction is off in that profile. The library refuses to stream while
redaction is enabled, because redaction cannot be guaranteed across chunk
boundaries, and the profile is process-wide with no per-call override — so a
deployment gets one or the other. The default keeps the whole documented
surface working.

| `pii_redaction.enabled` | Buffered | Streaming |
|---|---|---|
| `false` (shipped default) | works | works |
| `true` | works, redacted | 400 carrying the library's explanation |

Deployments handling personal data set the flag and accept that streaming
stops. The choice belongs to whoever configures the deployment, which is why
it is a profile setting.

## Cost-event capture

The library emits one structured log record per call (event type
`ai_api_call_cost`) on the logger named by its `emit_cost_topic` setting
(default `ai_api_unified.observability.cost`). At startup the service
attaches a handler to that topic. The service ships a JSON-lines file
handler; the destination is expected to become a metrics pipeline. Streaming calls
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

- **Unknown fields pass through.** Every non-standard attribute on the log
  record is carried into the JSON object; there is no allowlist to update, so
  a field the library adds later survives with no change here.
- **The topic records at `INFO` regardless of the root logger.** A deployment
  running the root at `WARNING` must not thereby drop every cost event.
- **A failing sink never raises into the request.** Write errors route
  through the logging error path, so a broken sink costs one event rather
  than the call that produced it.

## API versioning

- **URI major version** (`/v1/`): bumps only on breaking changes to request
  or response shapes. `/v1` and `/v2` may serve side by side during a
  migration window.
- **Service version** (semver): moves with ordinary releases, reported by
  `/healthz` and the OpenAPI document. See the README for the release process
  and the current value.
- The pinned library version is independent of both and reported by
  `/healthz` so operators can correlate service behavior with library
  releases.

## Deployment

The service ships as a container. `Dockerfile` builds a virtualenv in a builder
stage and copies it into a slim runtime that carries no build toolchain, no
Poetry, and no source for the tests or docs. It runs gunicorn with uvicorn
workers, as the stack section describes, and listens on `PORT`.

`.dockerignore` is load-bearing rather than tidiness. `docker build` ships the
whole directory as build context and `.gitignore` does not apply to it, so
without the exclusion a `.env` would land in an image layer readable by anyone
who can pull the image.

Cloud Run is the configured target. `make gcp-secrets` writes provider keys to
Secret Manager and grants the runtime service account access; `make gcp-deploy`
builds with Cloud Build, so no local Docker daemon is involved. Secrets mount
at runtime, so no key appears in the image or in the service's stored
environment.

One Cloud Run behavior shapes the code: **its frontend answers `/healthz`
itself and never forwards the request to the container.** The service therefore
serves the same health body at `/health`, which Cloud Run does not reserve.

Rate limiting is sized against the deploy: the counter is per process, so the
deploy target sets `WEB_CONCURRENCY=1` to keep the configured limit meaning
what it says.

## Cost attribution

`caller_context.py` puts caller identifiers into the library's observability
context for the life of a request, so the library stamps them onto the cost
event it emits. Without it every event carries the library's default caller and
a deployment serving many users sees one total.

The API key's label namespaces the caller id, because two applications
numbering their users from one would otherwise collide and the combined spend
would land on whichever the reader assumed.

`session_id` and `workflow_id` are passed twice: once as context fields, and
again as tags. The cost event carries a fixed field set that includes
`caller_id` and neither of the others, while tags are emitted on cost events as
`tag_<name>`. Passing them only as context fields attributes spend to a user
but leaves the session invisible in the record that bills.

The context lives in a contextvar and is reset in a `finally`, so a worker
thread cannot carry one request's caller into the next. It propagates into the
threadpool, which matters because the sync library calls are the ones emitting
cost events for token counting and model listing.

Identifiers are length-bounded and stripped of control characters. They reach
log lines and cost records, so a newline would let a caller forge an entry.

## Configuration loading

`config.py` loads `.env` into `os.environ` at startup, and real environment
variables win over the file.

The library reads `.env` for **its** settings on its own — `EnvSettings` is a
pydantic-settings model declared with `env_file=".env"` — but that populates
the model, not `os.environ`. The
service's own variables (`HTTP_API_KEYS`, `HTTP_COST_LOG_PATH`,
`HTTP_CORS_ORIGINS`, `LOG_LEVEL`) are read straight from `os.environ`, so
without this load a `.env` holding `HTTP_API_KEYS` leaves the service with no
keys configured at all.

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

- **`Retry-After` is not forwarded** on a 429. `AiProviderRequestError`
  carries the status code and not the provider's response headers, so the
  service has no value to pass on. Callers back off on their own schedule.
- **The token-limit 422 names the fix.** `StructuredResponseTokenLimitError`
  reports `minimum_supported_tokens` for the model, so the detail states the
  floor rather than leaving the caller to bisect `max_response_tokens`.

Handler registration order matters: every one of these inherits
`AiProviderError`, so the base is registered last. Registering it first would
collapse the whole table into one 502.

### Failures outside the library's hierarchy

Two failure classes fall outside the table above.

**Missing credentials.** Provider SDKs raise a plain `ValueError` naming the
variable ("ANTHROPIC_API_KEY environment variable must be set"), which is not
an `AiProviderError` and matches no handler. `clients.py` translates it at
construction, where the engine is still known, into
`ProviderNotConfiguredError` → **503**, with the variable name in the detail.
503 rather than a 4xx because the caller did nothing wrong and retrying will
not help until an operator acts.

**Everything else.** `ErrorEnvelopeMiddleware` converts any unhandled
exception into the same JSON error shape with a 500. It runs inside CORS and
outside the routes, so its responses carry CORS headers; Starlette's own
server-error handler sits outside CORS, and a response from there reaches a
browser with no `Access-Control-Allow-Origin` header and no readable reason.
The body never carries the traceback. It carries a request id that is also
written to the log line holding the traceback, so an operator can join the two
without the caller seeing internals.

## Risks

| Risk | Mitigation |
|---|---|
| SSE ties one thread per active stream | Size threadpool x workers deliberately; pursue async streaming upstream in the library. |
| Gemini client construction does a network call | Pool clients; construction failures surface at first use per (engine, model), not per request. |
| Double retries (library + service) amplifying latency | The service adds no retry layer; retries stay library/SDK-owned. |
| Cost events lost if the handler is misconfigured | Handler attachment is part of app startup; a startup check fails loudly if the topic has no handler. |
| `raw_content` breaks across library upgrades | Token is versioned alongside the service; conversation turns are short-lived. |

## Future work

- Voice, if a consumer asks for it.
- Library upstream: async streaming, a streaming conversation call, pluggable
  cost sink, cached config loading. None are blockers.
