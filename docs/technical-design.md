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

- **FastAPI**: Pydantic-native, so route schemas can wrap the library's
  Pydantic types. It generates the OpenAPI spec the TypeScript client is
  built from. It also handles the library's mixed sync and async surface:
  async routes await the library's `asend_*` coroutines, and sync-only paths
  run in the threadpool.
- **uvicorn** (dev) / gunicorn with uvicorn workers (production).

## Endpoint-to-library mapping

| Endpoint | Library call | Notes |
|---|---|---|
| `POST /v1/completions` | `asend_prompt(...)` | True coroutine; handler awaits it. Attachments travel in `other_params`, which both this and the streaming call accept. The library decides which MIME types are acceptable and validates list alignment and size caps. The service classifies the type and turns the library's refusal into a 400, with no allowlist of its own. `prompt_cache` travels in `other_params` too, so it reaches the streaming call as well. A request with only a cache hint still builds the parameters object. |
| `POST /v1/completions` + `stream: true` | `send_prompt_streaming(...)` | Sync generator bridged to SSE via `StreamingResponse` in the threadpool. No PII redaction (library rule). No retries on streams (library rule). A fallback chain fails a stream over before its first chunk only; the `done` event names the model that served (see "Model fallback"). |
| `POST /v1/structured` | `asend_structured_output(...)` | `data` is null on `length`/`refusal` finish reasons; surface `finish_reason` to the caller. `prompt_cache` maps to the library's `AIPromptCacheHint` keyword (2.30.0). |
| `POST /v1/conversations/turn` | `asend_conversation(...)` | Stateless: the caller sends the full history each turn and runs tools itself (ADR-0018 in the library repo). Before library 2.26.1, google-genai's own validator rejected a string `content`, so a Gemini caller got a 500 from this endpoint and from `/v1/structured`. 2.26.1 made the documented `{role, content}` history work on Gemini. The handler passes `prompt_cache` as the `AIPromptCacheHint` keyword. The `conversation_token` holds the messages the serving engine's `extend_messages_with_turn` appends for the turn (v2), so replay splices in that engine's wire shape; see "Model fallback". From 2.28.0 the library refuses a forced `tool_choice` on Claude Opus 5.5 and Fable 5.1 with a bare `ValueError` before the network call. The handler matches that error by the library's wording and turns it into a 400. Any other `ValueError` stays a 500. |
| `POST /v1/run-grants`, `GET` and `DELETE /v1/run-grants/{run_id}` | none | Service-owned; see "Run grants". Issuing resolves the pinned model through the client pool and refuses one without token rates. |
| `POST /v1/embeddings` | `agenerate_embeddings` / `agenerate_embeddings_batch` | |
| `POST /v1/tokens/count` | `count_tokens(...)` | Sync; threadpool. |
| `GET /v1/models` | `list_model_names` + `capabilities` + pricing registry | Read-only registry data: context windows, rates, lifecycle status, replacements. From library 2.26.1, `list_model_names` on Gemini queries the live catalog and can block and sleep, so the handler runs it in the threadpool. The library caches the answer per client for 15 minutes, so the pool holds one answer per engine, not one per request. |
| `POST /v1/batches` | `submit_batch(...)` | Sync; threadpool. Empty batches and duplicate `custom_id`s get 400s here. The library raises `ValueError` for both, which would surface as 500s. |
| `GET /v1/batches/{id}` | `get_batch(...)` | Sync; threadpool. Every batch call requires `engine`: a batch lives in one provider's account, so the id alone does not identify it. |
| `GET /v1/batches/{id}/results` | `get_batch_results(...)` | Sync; threadpool. Correlate by `custom_id`; provider order is not request order. Results include usage, with the cache reads and writes `AIBatchResultItem` reports from 2.30.0. They have no `usd_cost`: the registry's token rates are interactive rates, and batch bills at the provider's batch rate. |
| `POST /v1/batches/{id}/cancel` | `cancel_batch(...)` | Sync; threadpool. Cancellation is a request; already-processed items stay billed. |
| — | `run_batch(...)` | Deliberately not exposed: it blocks until the batch ends. Over HTTP that is a connection held for hours, and the work is lost if the connection drops. |
| `POST /v1/images` | `generate_images(...)` | Sync; threadpool. Bytes go to the artifact store and the response returns references. Base64 in JSON inflates the payload by a third, caps it at one buffer, and gives a caller no way to show progress or resume. |
| `POST /v1/videos` | `submit_video_generation(...)` | Starts a worker-thread job and returns at once. Generation outlives the request, so on Cloud Run this needs CPU allocated outside request processing. |
| `GET /v1/videos/{id}` | `get_video_generation_job(...)` via the job record | Progress comes from the shared store, not process memory: the instance a caller polls is usually not the one doing the work. |
| `GET /v1/videos/{id}/events` | none | Service-owned. Emits progress when it changes, and marks it `estimated` when the provider reports no figure, so the feature works without provider progress reports. |
| — | `AIFactory.get_ai_voice_client()` | Library 2.24.0 added this. Before, voice was built only through `AIVoiceFactory.create()`, so anyone reading the factory took voice to be unsupported. |
| `GET /v1/voices` | `list_available_voices`, `list_output_formats`, `common_vendor_capabilities` | Read from the client's own published fields, so a deployment that switches engines reports the truth with no code change. Paged at 200 with the true total returned, because one engine publishes thousands. |
| `POST /v1/speech` | `text_to_voice(...)` / `text_to_voice_with_emotion_prompt(...)` | Sync; threadpool. Called with keywords only. The concrete providers narrow the base signature and require `voice` and `audio_format`, which the base defaults to None. Emotion and SSML fail up front when the engine's published capabilities lack them, because an engine without them bills the call and silently ignores both. |
| `GET /v1/artifacts/{id}/content` | none | Service-owned. A client builds a progress bar from `Content-Length`. With `Range`, a failed transfer costs only a re-download. |
| `GET /healthz` | none | Reports service, API, and pinned library versions. |

## Client pool

`AIFactory.get_*` constructs a new provider client per call. Each
construction re-reads its configuration (`.env` and `models.json`). Since
library 3.6.0 it also probes the provider once to confirm the model is
reachable (`model_check` in models.json, memoized per model and credential
for the life of the process). The service therefore keeps a process-wide
dict of engine clients keyed by `(engine, model)`, built on first use and
reused. Completions keys add whether the client holds the fallback chain;
see "Model fallback". Reuse is safe: engine instances hold no
per-conversation state, provider SDK clients are thread-safe, and the
library's mutable instance attributes are idempotent lazy caches.

Implemented in `clients.py`.

- **`model=None` is its own pool entry**, separate from any named model, even
  when the engine default resolves to that same model. The pool keys on the
  caller's request, before the engine resolves a default.
- **Construction runs outside the lock.** Two requests racing on the same cold
  key can both build a client, and the pool discards the loser's copy.
  Holding the lock across construction would put every cold start behind one
  Gemini network round trip. A duplicate build is harmless because the
  clients hold no state worth keeping.

A construction failure leaves the key cold, so a request that arrives after
someone fixes the misconfiguration succeeds without a restart.


## Model fallback

Library 2.32.0 added a fallback chain (pinned now at 3.22.0). When
`completions.fallbacks` in models.json, or a `fallbacks=` argument, names
candidates, `AIFactory.get_ai_completions_client` returns an
`AiFallbackCompletions` that wraps the primary engine. A request that fails
with a `fallback_reason` listed in `completions.fallback_on` retries on the
next candidate. The chain logic is the library's. The service owns the parts
below.

**Which clients hold the chain.** The factory reads the setting whenever
`fallbacks` is None. Left to the factory, every pooled client would get the
chain, including the ones batches, token counts, and the model catalog use.
The service always passes `fallbacks` explicitly: the parsed chain for the
three generation routes, `[]` for everything else. It reads the chain through
the library's own settings resolver (`EnvSettings.get_setting`), which checks
the process environment, then `.env`, then `completions.fallbacks` in
models.json. Reading `os.environ` directly, as the service did before
models.json existed, would leave a deployment that moved its chain there with
no fallback at all. The completions pool key grows a third part,
`(engine, model, fallback)`, because the wrapper and the plain engine are
different objects. The service drops a candidate naming exactly the
requested model from that request's chain, since retrying an overloaded
model on itself spends a second call on the same failure. A request's
`"fallback": false` selects the plain entry.

**Which model served.** Responses report the engine and model that answered,
which can differ from the ones requested, plus `served_by_fallback`. Every
source is per call. The wrapper's `last_route` is per-client state on a
client every request shares, so a stream, or a call that ran in the
threadpool, can read it after another request has moved it.

- Conversation turns and structured calls read the route stamp the wrapper
  puts on `AITurnResult` and `AIStructuredOutputResult`.
- A buffered text prompt goes through `asend_prompt_with_result` (library
  3.16.0). Its `AIPromptResult` has the same `provider_engine` and
  `model_name`, filled from the wrapper's per-call ledger.
- A stream has no envelope. `streaming.py` opens the library's
  `request_cost_scope` around the whole stream, and the wrapper joins it.
  Each provider call made for the stream lands on the scope as a line item.
  The report that closes the scope names the call whose answer the stream
  returned (`served_by_engine`, `served_by_model`). The scope lives in a ContextVar,
  which Starlette copies onto the threadpool worker for each step.

`fallback.served_route` compares what the library reported with the chain's
resolved primary. A plain client (no chain) can only have served itself,
whatever it reports. Before 3.7.0 the service read the wrapper's
`served_by_fallback` log record. The wrapper writes that record only when a
candidate fails during the call. With cooldowns, a request can start on a
fallback because the primary is resting, with no failure and no record, so
the log-based service reported that request as served by the primary. The
envelope and the scope cover both cases.

`parse_fallback_candidates` and the provider registry's
`get_ai_provider_spec` are public module-level names in the library. The
service imports them from their submodules because the package root does not
re-export them.

**Cost.** The wrapper reports the primary's capabilities, pricing included.
So the service prices `usd_cost` for a fallback-served call at a plain pooled
client for the fallback's engine and model. Cost events need nothing: each
engine emits its own, so the sink already records the fallback's engine and
model.

**Startup check.** The library validates fallback engine tokens when it builds
a client, which here is the first request per pool key. A typo would
therefore surface as failed requests during the first outage.
`verify_fallback_config` runs at startup. It checks every engine token
against the provider registry and every reason against `AiFallbackReason`,
refuses to start on either error, and logs the chain.

**Conversation tokens (v2).** A v1 token held the turn's `raw_content`, and
replay wrapped it as the content of the caller's assistant message. Only
Anthropic and Bedrock use that shape. OpenAI's `raw_content` is a whole
message and Gemini's is a parts list, so neither ever replayed, and a
conversation that failed over to OpenAI broke on its second turn. A v2 token
holds the messages the serving engine's own `extend_messages_with_turn`
appends for the turn (a fallback client routes that call by the turn's
stamp). Replay splices them in place of the caller's message. A model
without tool-use support refuses `extend_messages_with_turn`, so the service
still encodes its turn as v1. The service still accepts v1 tokens.

**Library 2.33.0 to 2.35.0.** The service passes these through with no logic
of its own beyond the error-body fields above:

- 2.33.0 adds `auth_rejected` to the default reasons and skips the rest of an
  account's candidates after an account-wide failure. The service's startup
  check and log line take the default set from the library, so they follow.
- 2.34.0 adds an opt-in `timeout` reason. It works only with a short deadline
  and no SDK retries, so `/v1/structured` and `/v1/conversations/turn` take
  `retry_policy`, sent as the library's reserved `provider_options` key. The
  library has no per-call retry setting for text-prompt calls, so
  `/v1/completions` relies on the deployment's `completions.retry_policy`.
  That route does not offer the field, because a per-request value the
  library ignored would be worse than none.
- 2.35.0 lets a turn with engine-shaped history fail over within that engine
  family. Nothing changes here. The route stamp already names the model that
  served, and `served_by_fallback` compares it with the requested primary, so
  an Opus turn served by Sonnet reports Sonnet and prices at it.

**Library 3.0.0 to 3.22.0.** One pin step, taken in service 1.13.0. The
service adopted these changes:

- 3.2.0 and 3.11.0 move every model selection and the middleware profile into
  `models.json`. The service ships `config/models.json` and defaults
  `AI_MODELS_CONFIG` to it (`cost.apply_default_models_config`), replacing
  the middleware YAML and `AI_MIDDLEWARE_CONFIG_PATH`. The deprecated
  environment keys still win over the file, with a warning.
- 3.5.0 turns thinking on at `medium` by default. The three generation routes
  take `thinking`, sent as `other_params.thinking` on the text call and as the
  reserved `thinking_level` provider option on the others.
- 3.7.0 benches a failed candidate. That broke the log-record route tracking
  (above), which 3.16.0's envelope and the request cost scope replaced.
- 3.16.0 and 3.17.0 fill engine, model, usage, and cost on every result, so
  `/v1/completions` now reports `usage` and `usd_cost`.
- 3.18.0 raises `AiProviderRequestError` for media failures. The service
  still classifies unwrapped SDK errors itself (1.6.1) as the backstop.

Not exposed yet: the complexity router and route preview (3.0.0, 3.15.0),
per-call model pins (3.16.0), classification as a capability, cost
attribution by `tenant` and `purpose` (3.14.0), and the per-environment
overlay beyond passing `AI_MODELS_ENV` through. Each needs its own
request-schema change.

**Library 3.23.0 and 3.24.0.** 3.23.0 (service 1.14.0) adds the response
cache, on in the shipped profile, with `cache_hit` and `usd_saved` on the
generation responses and two admin endpoints. 3.24.0 (service 1.15.0) adds
guardrails, off in the shipped profile. A blocking check raises
`AiGuardrailBlockedError`. It is an `AiProviderError` but not an
`AiProviderRequestError`: it has no `fallback_reason`, no chain moves it, and
without its own handler it would fall through to the base's 502. `errors.py`
maps it to 422 `guardrail_blocked` with `guardrail_check` and
`guardrail_direction` on the body. The handler builds the detail from the
exception's fields and never from its message, so the checked text has no
path into a response or a log line. A flagging check puts its name in
`guardrail_flags` on every result envelope, and the three generation
responses copy it. A stream runs the input checks only, and where the block
surfaces depends on the pooled client. The library wraps an engine's
`send_prompt_streaming` so the check runs when the route opens the stream,
before `StreamingResponse` exists, and the block is the ordinary 422. A
fallback chain opens each candidate's stream inside its own generator. There
the check runs at the first `next()`, inside the SSE bridge after the 200 is
committed, and `streaming.py` reports it as an `error` event with
`guardrail_blocked` and the same fields.

**Library 3.25.0 and 3.26.0.** One pin step, service 1.16.0. 3.25.0 adds
four moderation backends for the guardrail checks (Mistral, Azure Content
Safety, Bedrock Guardrails, Google Cloud Natural Language), chosen by
`backend` or by `auto` in a fixed order. The service passes them through and
ships only `settings.backends.timeout_seconds`. It leaves the provider blocks
out, since a `guardrail_id` or an endpoint changes what `auto` picks. 3.26.0
stops reporting a construction-time configuration error as an unsupported
engine. It raises `AiProviderConfigurationValueError` with the provider's
message instead: see "Error mapping".

**Library 3.26.1 through 3.28.0.** One pin step, service 1.17.0. 3.26.1
and 3.27.0 change only Google service-account (Vertex) auth: the library
loads the key file named in `.env`, and the location defaults to `global`.
The service authenticates Google with an API key, so neither reaches it.
3.28.0 gives `prompt_injection` a separate pass over tool results. It judges
each new tool result in a conversation alone, as data that should hold no
instructions for the assistant. The library recognizes tool results by each
engine's own shape, which callers already send in `messages`.
`AiGuardrailBlockedError` gains `source` and `tool_call_ids`, and
`AITurnResult` gains `guardrail_flagged_tool_calls`. The service copies them
onto the 422 body (`guardrail_source`, `guardrail_tool_call_ids`), the
stream's `error` event, and the turn response. The ids are the caller's own
identifiers and hold no checked text, so they keep the rule that no checked
text reaches a response or a log line.

**Library 3.29.0 and 3.29.1.** One pin step, service 1.17.1, taken for
3.29.1. On 3.28.0, Gemini tool-use turns failed with a 500 after Gemini had
billed them. The library kept each part's `thought_signature` as raw bytes,
and the response cache's JSON dump raised on them. 3.29.1 stores the
signature as base64 text, which google-genai decodes on replay. Its cache
also skips a result it cannot store and returns the result. 3.29.0 adds the
harness result contract (`RUN_FAILED`, `exception.cost`, reported costs,
`route_confidence`). The service does not expose it yet.

**Why the service needed 2.32.1.** On 2.32.0 a turn that failed over from
Claude to OpenAI broke the next turn. The OpenAI engine saved a text-only
turn as `{"role": "assistant", "content": "...", "annotations": []}`. The
wrapper read that history as provider-neutral and started the next turn at
Claude. Anthropic rejected the `annotations` key with a 400, which is not a
fallback reason (ai-api-unified#65). 2.32.1 saves a text-only turn as plain
`{"role", "content"}` and recognizes OpenAI SDK fields as OpenAI history. A
live check on 2.32.1 confirmed it: the second turn after a failover gets an
answer, and an OpenAI turn replays on a healthy Claude. The service does not
rewrite engine-shaped messages itself; replay shape stays the library's.

## Authentication

Implemented in `auth.py`. Provider credentials live only in the service
environment, so every accepted request spends money the caller never had to
hold a key for. An unauthenticated endpoint here lets anyone who reaches it
spend the service's provider credit.

Callers send a shared secret in `Authorization: Bearer <key>`, configured
through `HTTP_API_KEYS` as comma-separated `label:key` entries. Several keys
can be live at once, so a caller can be rotated or revoked alone, and the
label names the calling application in logs. The label authenticates
nothing.

- **Enforced as middleware.** Middleware protects a new route the moment it
  exists, so adding an endpoint cannot quietly expose paid capacity through a
  forgotten decorator. A test asserts that every `/v1` path in the OpenAPI
  spec answers 401 without a key.
- **CORS wraps auth.** Starlette runs the last-added middleware outermost, so
  the app registers CORS after auth. A 401 has CORS headers, so a browser
  caller can read the body that explains the missing key.
- **`OPTIONS` is never gated.** Browsers do not attach `Authorization` to a
  preflight, so gating it would make every cross-origin call fail before it
  could authenticate.
- **Key comparison uses `hmac.compare_digest`** against every configured key,
  so the time taken does not depend on how far the key matched.

Startup raises `AuthNotConfiguredError` when no keys are set and no explicit
opt-out was given. This stops a deployment that never set keys from serving
paid endpoints to anyone who can reach the port. `HTTP_AUTH_DISABLED=1` is
the deliberate opt-out and logs a warning on every start.

This is not a user identity system. Keys name calling applications, and the
service stores no per-key state beyond the label. Per-user identity, quotas,
and per-caller rate limits belong behind a real identity provider.

## Run grants

Implemented in `run_grants.py` (the store), `routes_run_grants.py` (issue,
read, revoke) and the conversation route (pin and spend). The meta-harness
runs an agent in a sandbox, and the agent's model calls come here. Handing the
sandbox an API key would give the agent every model, unlimited spend and no
end date. A **run grant** narrows that to one run.

A grant is a record in Redis: `run_id`, the issuer's key label, the pinned
engine and model, `spend_limit_usd`, `expires_at`, and the attribution fields
for the run's cost events. The issuer calls `POST /v1/run-grants` with its API
key and gets a **grant key** (`rg_` plus 32 random bytes, URL-safe base64).
The grant key carries no data; it is the lookup key for the record. The
sandbox presents it as its bearer.

| Key | Holds |
|---|---|
| `<prefix>grant:<run_id>` | The grant, as JSON |
| `<prefix>grant-key:<sha256>` | The run id a grant key belongs to |
| `<prefix>grant-keys:<run_id>` | The digests of the grant's keys, for revocation |
| `<prefix>grant-spend:<run_id>` | Spend so far, in whole picodollars |

Every key expires at the grant's `expires_at` (Redis `EXAT`), so an
unrevoked grant cleans itself up.

Decisions:

- **Redis, and optional.** The service runs as several gunicorn workers and
  Cloud Run instances, and a grant issued by one has to work on all of them.
  Process memory cannot do that. Only a deployment that sets `HTTP_REDIS_URL`
  connects; without it the grant endpoints answer 503 and nothing else
  changes. A Redis that is down at startup is logged, and grant requests
  answer 503 `run_grant_store_unavailable` until it returns. Requests with
  API keys are unaffected.
- **Hashed keys.** Redis holds the SHA-256 of each grant key, so a copy of
  the keyspace holds nothing that authenticates. A grant key is 256 bits of
  randomness, so an unsalted hash is enough.
- **Opaque, not signed.** A signed token such as a JWT would carry the grant
  itself, but the spend changes on every turn and must live on the server
  anyway, and a signed token cannot be revoked without a server-side list.
- **The authenticator resolves it.** `ApiKeyAuthMiddleware` sees the `rg_`
  prefix, looks the key up, and allows only `POST /v1/conversations/turn`
  (403 elsewhere). It sets the issuer's label as the request's key label and
  puts the grant on `request.state.run_grant`. A grant key is checked even
  when `HTTP_AUTH_DISABLED=1`, since its limits apply either way.
- **The grant pins the model.** The turn route uses the grant's engine and
  model and ignores the body's, so `engine` is optional on the turn request.
  The body's `fallback` flag still applies, so a turn can fail over along the
  deployment's chain. The model is resolved when the grant is issued, and a
  model without token rates is refused, since its spend could not be counted.
- **Spend is checked before the turn and counted after it.** A turn's cost
  is known only once the provider answers. The route refuses a turn with 402
  `run_grant_spend_limit` when the run's spend has reached the limit, and
  otherwise adds the turn's `usd_cost` with `INCRBY`. A run can therefore pass
  its limit by the turns in flight when it crossed. A cache hit adds zero. A
  fallback turn on an unpriced model adds nothing and logs a warning. A turn
  that fails after the provider billed it is not counted, because the service
  never learns its cost. A turn still running when its grant is revoked
  finishes, since the service does not stop a provider call when the caller
  goes away. Its cost lands after the revoke, so the revoke's `spent_usd`
  leaves it out. Its cost event still carries the run's `run_id` tag, and the
  spend key it writes expires at the grant's `expires_at`. The meta-harness
  cancel demo shows this: the revoke reported $0 for a turn the provider
  billed.
- **Exact money.** Spend is an integer count of picodollars, rounded up per
  turn, so the total never drifts the way a float would.
- **Retry-safe.** Asking again for a live `run_id` with the same issuer,
  engine, model and limit returns a new key for the same grant, and the old
  key keeps working. A caller whose first request timed out can retry without
  breaking a sandbox that already holds a key. Other terms answer 409.
  Revoking a grant that is gone answers 200 with `revoked: false`.
- **The grant attributes the cost.** The caller-context middleware takes
  `caller_id`, `session_id`, `workflow_id` and `tags` from the grant, adds a
  `run_id` tag, and ignores the attribution headers. The sandbox sends the
  request, and what it says about itself is not something the issuer vouched
  for.
- **Each run has its own rate-limit bucket** (`run:<run_id>`), so a fleet of
  runs from one issuer does not share the issuer's budget. The spend limit
  bounds each run's cost.

Not built yet: the router's band. A request with a `route` hint on a grant
would switch that call from the pinned model to a tier band capped at the
pinned model's tier (meta-harness technical design, section 6). The service
does not expose the router yet, so a grant only pins.

## Conversation `raw_content` round-trip

`AITurnResult.raw_content` is opaque provider-SDK content the library needs
back verbatim on the next turn. The service serializes it into an opaque
string token in the turn response, and clients store and echo it untouched.
The token's encoding is internal to the service and may change between
service versions. Clients must never parse it.

Implemented in `conversation_token.py` as `v1.<base64(compact JSON)>`.

The token is **neither signed nor encrypted**, by design. The content is the
caller's own conversation replayed back to them, so it holds nothing they did
not already send or receive. Signing would add key management and rotation
for no confidentiality gain, and the service still treats a decoded token as
untrusted input.

The version prefix makes an outdated token fail cleanly. When the encoding
changes, the service rejects an old token with a message telling the caller
to start a new conversation, so the token never reaches a provider as
malformed content. Decoding happens before the service touches the client
pool, so a bad token costs no provider call.

Tokens are decoded **in place**, wherever the caller put them: the service
expands an assistant message whose content matches `v<digits>.` to the
provider content it encodes. The service does not append the previous turn
itself, because it cannot know where a new user message belongs relative to
it. Appending would turn `[user, assistant, user]` into
`[user, user, assistant]` and reorder the conversation. Any version-shaped
prefix counts as a token attempt, so the service rejects a token from a
retired version with the same clear message.

## Middleware profile

The library reads one process-wide middleware list. It comes from the
`middleware` section of models.json, or from a YAML profile when the
deprecated `AI_MIDDLEWARE_CONFIG_PATH` is set, which wins. The service ships
the list inside `config/models.json` and defaults `AI_MODELS_CONFIG` to that
file when unset, so a stock deployment starts with observability and cost
emission already on. `verify_cost_capture` reads the library's resolved
settings, so it checks whichever source the library is using.

PII redaction is off in that profile. The library refuses to stream while
redaction is on, because redaction cannot be guaranteed across chunk
boundaries. The profile is process-wide with no per-call override, so a
deployment gets one or the other. The default keeps the whole documented
surface working.

| `pii_redaction.enabled` | Buffered | Streaming |
|---|---|---|
| `false` (shipped default) | works | works |
| `true` | works, redacted | 400 with the library's explanation |

Deployments handling personal data set the flag and accept that streaming
stops. The choice belongs to whoever configures the deployment, which is why
it is a profile setting.

## Cost-event capture

The library emits one structured log record per call (event type
`ai_api_call_cost`) on the logger its `emit_cost_topic` setting names
(default `ai_api_unified.observability.cost`). At startup the service
attaches a handler to that topic. The service ships a JSON-lines file
handler; the destination should become a metrics pipeline. Streaming calls
emit their cost event when the stream ends, including streams the client
abandoned (the library handles `GeneratorExit`).

Implemented in `cost.py`, attached and verified by the app lifespan.

| Variable | Default | Purpose |
|---|---|---|
| `HTTP_COST_LOG_PATH` | `cost-events.jsonl` | Sink file; startup creates parent directories |
| `HTTP_COST_TOPIC` | `ai_api_unified.observability.cost` | Logger to attach to; set it when a deployment changes the library's `emit_cost_topic` |

Startup attaches the handler, then calls `verify_cost_capture()`, which raises
`CostEventNotCapturedError` when the topic has no handler. The check also
catches a deployment that changed the library's `emit_cost_topic` without
telling the service. There, capture would attach to a topic nothing
publishes to, and every event would vanish. There is no opt-out, because hard
requirement 3 admits none.

- **Unknown fields pass through.** Every non-standard attribute on the log
  record goes into the JSON object. With no allowlist to
  update, a field the library adds later survives with no change here.
- **The topic records at `INFO` regardless of the root logger.** A deployment
  running the root at `WARNING` must not drop every cost event as a side
  effect.
- **A failing sink never raises into the request.** Write errors go through
  the logging error path, so a broken sink costs one event and never the call
  that produced it.

## API versioning

- **URI major version** (`/v1/`): bumps only on breaking changes to request
  or response shapes. `/v1` and `/v2` may serve side by side during a
  migration window.
- **Service version** (semver): moves with ordinary releases, reported by
  `/healthz` and the OpenAPI document. See the README for the release process
  and the current value.
- The pinned library version is independent of both. `/healthz` reports it so
  operators can correlate service behavior with library releases.

## Deployment

The service ships as a container. `Dockerfile` builds a virtualenv in a builder
stage and copies it into a slim runtime with no build toolchain, no Poetry,
and no source for the tests or docs. It runs gunicorn with uvicorn workers, as
the stack section describes, and listens on `PORT`.

`.dockerignore` keeps `.env` out of the image. `docker build` ships the whole
directory as build context, and `.gitignore` does not apply to it. Without the
exclusion, a `.env` would land in an image layer that anyone who can pull the
image can read.

Cloud Run is the configured target. `make gcp-secrets` writes provider keys to
Secret Manager and grants the runtime service account access. `make gcp-deploy`
builds with Cloud Build, so no local Docker daemon is needed. Secrets mount at
runtime, so no key appears in the image or in the service's stored
environment.

One Cloud Run behavior shapes the code: **its frontend answers `/healthz`
itself and never forwards the request to the container.** The service therefore
also serves the health body at `/health`, which Cloud Run does not reserve.

Rate limiting depends on the deploy. The counter is per process, so the
deploy target sets `WEB_CONCURRENCY=1` to keep the configured limit meaning
what it says.

Run grants need a Redis every instance can reach. On Google Cloud that is
Memorystore for Redis on the service's VPC, with `HTTP_REDIS_URL` pointing
at it. `make gcp-deploy` does not provision one; a deployment without it
serves everything except `/v1/run-grants`.

## Cost attribution

`caller_context.py` puts caller identifiers into the library's observability
context for the life of a request, so the library stamps them onto the cost
event it emits. Without it, every event has the library's default caller, and
a deployment serving many users sees one total.

The API key's label namespaces the caller id. Two applications that number
their users from one would otherwise collide, and the combined spend would
land on whichever application the reader assumed.

The service passes `session_id` and `workflow_id` twice: once as context
fields, and again as tags. The cost event has a fixed field set that includes
`caller_id` and neither of the others, while the library emits tags on cost
events as `tag_<name>`. Passing them only as context fields attributes spend
to a user but leaves the session out of the record that bills.

The context lives in a contextvar, and a `finally` resets it, so a worker
thread cannot pass one request's caller into the next. It propagates into the
threadpool. That matters because the sync library calls emit the cost events
for token counting and model listing.

Identifiers are length-bounded and stripped of control characters. They reach
log lines and cost records, so a newline would let a caller forge an entry.

## Configuration loading

`config.py` loads `.env` into `os.environ` at startup, and real environment
variables win over the file. `cost.apply_default_models_config` then points
`AI_MODELS_CONFIG` at `config/models.json` when the variable is unset. It
leaves alone a deployment that names its own file, or sets the variable blank
to run without one.

The library reads `.env` for **its** settings on its own: `EnvSettings` is a
pydantic-settings model declared with `env_file=".env"`. That fills the model
and leaves `os.environ` unchanged. The service reads its own variables
(`HTTP_API_KEYS`, `HTTP_COST_LOG_PATH`, `HTTP_CORS_ORIGINS`, `LOG_LEVEL`)
straight from `os.environ`. Without this load, a `.env` holding
`HTTP_API_KEYS` leaves the service with no keys configured at all.

## Error mapping

Implemented in `errors.py`. The rule is whose fault the failure is: a caller
who can fix the request gets a 4xx, and anything they cannot act on becomes a
5xx. Provider credentials live only in the service environment. So a
provider auth rejection, which arrives with the provider's status code, is a
service misconfiguration. Returning that status verbatim would tell the
caller to fix a key they do not hold.

`AiProviderRequestError` has the provider's own `status_code` and
`provider_engine`, so classification reads them directly and never matches
message text.

A client that cannot be built is the deployment's problem, so it is a 503
`provider_not_configured`. The pool catches `ValueError` around construction
and lifts the variable a provider's missing-key message names. Library
3.26.0's `AiProviderConfigurationValueError` (a retired default model, a base
URL that would send keys over plaintext) is a `ValueError` too and takes the
same 503, with no variable lifted. Its message names the models.json field
and the fix, and its first upper-case word is the deprecated environment key,
which "set it" would wrongly advise. It also subclasses
`AiProviderConfigurationError`, which answers 400 as the caller's fault, so
it has its own handler, registered before its base, for the case where it
escapes the pool. A fallback candidate that cannot be built never reaches
either: the chain logs `candidate_unbuildable` and skips it. A request naming
a retired model also gets a 503 on this path, although the caller could fix
it, because the error does not say whether the model came from the request
or the deployment.

| Library exception | Condition | HTTP status | `error` code |
|---|---|---|---|
| `AiProviderRequestError` | provider 429 | 429 | `provider_rate_limited` |
| `AiProviderRequestError` | provider 401 / 403 | 502 | `provider_auth_failed` |
| `AiProviderRequestError` | provider 5xx | 502 | `provider_error` |
| `AiProviderRequestError` | other provider 4xx | 400 | `provider_rejected_request` |
| `AiProviderRequestError` | no status, `fallback_reason` `timeout` (the call's deadline passed) | 504 | `provider_timeout` |
| `AiProviderRequestError` | no status (connection error) | 502 | `provider_unavailable` |
| `AiProviderConfigurationError` | retired model, bad config | 400 | `invalid_request` |
| `AiProviderCapabilityUnsupportedError` | engine cannot do it | 400 | `invalid_request` |
| `AiProviderDependencyUnavailableError` | provider SDK not installed | 503 | `provider_dependency_unavailable` |
| `StructuredResponseTokenLimitError` | response truncated by budget | 422 | `structured_response_token_limit` |
| `AiProviderError` | anything else in the hierarchy | 502 | `provider_error` |
| Request validation | malformed body | 422 | FastAPI default |

Every mapped failure returns the same body: `error`, `detail`, `engine`,
`provider_status`, `fallback_reason` (the library's `AiFallbackReason`
value from 2.31.0, or null), and `account_wide`. `account_wide` (from 2.33.0)
says whether every model on that provider account would fail the same way.
It is null when there is no reason, since the library's default then says
nothing about the account.

The service splits a timeout out from other status-less failures because
2.34.0 labels it. Before that, a client-side timeout and a refused connection
were the same 502. A 504 tells a caller the provider was slow, not down,
which shows whether a longer `request_timeout_seconds` would help.
`provider_status` is the provider's status when it reported one, and never
equals the response status: a provider 500 surfaces as a 502 here.

- **`Retry-After` is not forwarded** on a 429. `AiProviderRequestError`
  holds the status code but not the provider's response headers, so the
  service has no value to pass on. Callers back off on their own schedule.
- **The token-limit 422 names the fix.** `StructuredResponseTokenLimitError`
  reports `minimum_supported_tokens` for the model, so the detail states the
  floor and the caller need not bisect `max_response_tokens`.

Handler registration order matters. Every one of these inherits
`AiProviderError`, so the app registers the base last. Registering it first
would collapse the whole table into one 502.

### Failures outside the library's hierarchy

These failures fall outside the table above.

**Missing credentials.** Provider SDKs raise a plain `ValueError` naming the
variable ("ANTHROPIC_API_KEY environment variable must be set"). It is not an
`AiProviderError` and matches no handler. `clients.py` translates it at
construction, where the engine is still known, into
`ProviderNotConfiguredError` → **503**, with the variable name in the detail.
The status is 503 and not a 4xx because the caller did nothing wrong, and
retrying will not help until an operator acts.

**Everything else.** `ErrorEnvelopeMiddleware` converts any unhandled
exception into the same JSON error shape with a 500. It runs inside CORS and
outside the routes, so its responses have CORS headers. Starlette's own
server-error handler sits outside CORS, and its responses reach a browser
with no `Access-Control-Allow-Origin` header and no readable reason. The body
never includes the traceback. It includes a request id that the service also
writes to the log line holding the traceback, so an operator can join the two
and the caller sees no internals.

## Risks

| Risk | Mitigation |
|---|---|
| SSE ties up one thread per active stream | Size threadpool x workers on purpose; pursue async streaming upstream in the library. |
| Gemini client construction does a network call | Pool clients; construction failures surface at first use per (engine, model), not per request. |
| Double retries (library + service) amplifying latency | The service adds no retry layer; retries stay library/SDK-owned. |
| Cost events lost if the handler is misconfigured | Handler attachment is part of app startup; a startup check fails loudly if the topic has no handler. |
| `raw_content` breaks across library upgrades | Token versions follow the service; conversation turns are short-lived. |

## Future work

- Voice, if a consumer asks for it.
- Library upstream: async streaming, a streaming conversation call, pluggable
  cost sink, cached config loading. None are blockers.
