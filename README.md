# ai-api-unified-http 0.6.0

HTTP interface to the [ai-api-unified](https://github.com/davecthomas/ai-api-unified)
Python library, for web apps and other non-Python consumers. One
implementation of provider logic, pricing, model lifecycle, and middleware —
exposed over REST, with TypeScript clients generated from the OpenAPI spec.

Status: **in progress**. `/v1/completions` (buffered and streaming),
`/v1/structured`, and `/v1/conversations/turn` are live. `/v1/embeddings`,
`/v1/tokens/count`, and `/v1/models` still return `501 Not Implemented`.
See [docs/requirements.md](docs/requirements.md) for scope and
[docs/technical-design.md](docs/technical-design.md) for the architecture and
the endpoint-to-library mapping.

## Setup

Requires Python `>=3.11,<3.14` and [Poetry](https://python-poetry.org/). The
service installs the provider SDKs it needs (`anthropic`, `google-gemini`,
`openai`) as extras of the pinned library, so no separate provider install is
required.

```bash
poetry install --extras dev

# Configuration. Either copy a working .env from a sibling ai_api_unified
# checkout, or start from the template and fill in the providers you use.
# .env is gitignored — real keys never get committed.
make env            # copies ../ai_api_unified/.env or ../sample_ai_api_unified/.env
cp env_template .env    # or start from scratch
```

`make env` refuses to overwrite an existing `.env`. Provider variable names are
identical to the library's, so a `.env` that works there works here unchanged —
review `COMPLETIONS_MODEL_NAME` after copying, since a model name from a
different engine reaches the wrong provider and returns 404.

Variable names in `.env` are identical to the ai-api-unified library's, so a
`.env` that works for the library works here unchanged. See the comments in
[`env_template`](env_template) for every supported variable.

### Authentication

Every `/v1` endpoint spends provider credits, and callers never hold provider
keys, so an unauthenticated endpoint here is an open tab. The service
**refuses to start** unless authentication is configured or explicitly turned
off.

Set `HTTP_API_KEYS` to comma-separated keys. Each entry is `label:key` or a
bare `key`; the label names the calling application in logs and authenticates
nothing on its own. Several keys can be live at once, so a caller can be
rotated or revoked without disturbing the rest.

```bash
HTTP_API_KEYS="webapp:$(openssl rand -hex 32),batch:$(openssl rand -hex 32)"
```

Callers present the key as a bearer token:

```bash
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/models
```

`/healthz`, `/openapi.json`, `/docs`, and `/redoc` are served without a key.
Health has to answer load balancers that hold no credential, and the OpenAPI
document is what the TypeScript client is generated from.

Authentication runs as middleware rather than a per-route dependency, so a new
route is protected by existing. `HTTP_AUTH_DISABLED=1` turns it off for local
work; the service starts with a warning that every caller can spend credits.

### Middleware, and why streaming and redaction cannot both be on

The library reads one process-wide middleware profile from
`AI_MIDDLEWARE_CONFIG_PATH`, which the service defaults to
[`config/middleware.yaml`](config/middleware.yaml). Out of the box that profile
enables observability with cost emission and leaves **PII redaction off**.

The library refuses to stream while redaction is enabled, because redaction
cannot be guaranteed across chunk boundaries:

> Streaming completions are unavailable while PII redaction middleware is
> enabled: redaction cannot be guaranteed across stream chunk boundaries.

The profile is process-wide with no per-call override, so a deployment gets one
or the other:

| `pii_redaction.enabled` | Buffered completions | Streaming completions |
|---|---|---|
| `false` (default) | work, unredacted | work |
| `true` | work, redacted | 400 with the library's explanation |

A deployment handling personal data flips that flag and accepts that streaming
stops. `docs/technical-design.md` covers the trade in full.

### Cost-event capture

Every provider call emits a cost event carrying its own cost attribution, and
the service records it. It attaches a handler to the library's cost topic at
startup and **refuses to start when nothing is listening**, because an event
that goes nowhere is spend with no record of who incurred it.

The defaults work with no configuration: events land in `cost-events.jsonl`
(gitignored) as one JSON object per line. Set `HTTP_COST_LOG_PATH` to move the
sink, and `HTTP_COST_TOPIC` only if the library's `emit_cost_topic` was
retuned — otherwise capture attaches to a topic nothing publishes to.

Startup failure looks like this, and names the fix:

```
CostEventNotCapturedError: No handler is attached to the cost topic ...
```

## Run

```bash
make serve          # service on http://localhost:8080 (PORT=... to change)
```

`make help` lists every target: `install`, `lint`, `test`, `serve`, `smoke`,
and `webapp`. Without make:

```bash
poetry run uvicorn ai_api_unified_http.app:create_app --factory --reload --port 8080
```

Then:

- `http://localhost:8080/healthz` — liveness plus service, API, and library versions
- `http://localhost:8080/docs` — interactive API docs
- `http://localhost:8080/openapi.json` — the spec TypeScript clients are generated from

## API surface (v1)

| Method | Path | Purpose | Status |
|---|---|---|---|
| POST | `/v1/completions` | Text completion; `"stream": true` for SSE | **live** |
| POST | `/v1/structured` | Schema-validated structured output | **live** |
| POST | `/v1/conversations/turn` | One stateless tool-capable conversation turn | **live** |
| POST | `/v1/embeddings` | Embedding vectors | 501 |
| POST | `/v1/tokens/count` | Provider-side token count | 501 |
| GET | `/v1/models` | Model catalog: capabilities, pricing, lifecycle | 501 |
| GET | `/healthz` | Liveness and versions | live |

Every `/v1` path requires a bearer token; `/healthz` and the OpenAPI documents
do not.

### Conversations are stateless

The service holds no conversation state. Each turn carries the full history,
and the caller executes any tools the model asks for — the service never runs
one. A turn response includes `conversation_token`. Replay it on the next turn by
placing it as the content of an assistant message, in the position that turn
occurred:

```json
{"messages": [
  {"role": "user", "content": "first question"},
  {"role": "assistant", "content": "v1.W3siY2l0YXR..."},
  {"role": "user", "content": "follow up"}
]}
```

Ordering is yours, because only you know where a new user message belongs
relative to the previous assistant turn. Echo the token **without parsing
it**. It carries
provider-specific content whose shape changes with the engine and the library
version, so reading it would turn a provider's internal representation into
this service's contract. A token from an older service version is rejected
with a 400 telling you to start a new conversation.

## Versioning

Two independent version tracks:

- **API version** — the `/v1/` URI prefix. Bumps only on breaking changes to
  request or response shapes; `/v1` and `/v2` can run side by side during a
  migration.
- **Service version** — semantic versioning; the current value is the title of
  this file.
  - **patch** — bug fixes, internal changes, dependency pins
  - **minor** — new endpoints or fields, new provider support via a library
    upgrade
  - **major** — breaking deploy/config changes (an API-shape break also bumps
    the URI version)

The service version lives in exactly three places, bumped together, always:

1. `pyproject.toml` — `version = "X.Y.Z"` under `[project]`
2. `src/ai_api_unified_http/__version__.py` — `__version__: str = "X.Y.Z"`
3. `README.md` — the title heading on line 1

`tests/test_version_sync.py` fails whenever the three disagree. The pinned
`ai-api-unified` library version is a separate concern, visible in
`pyproject.toml` and reported by `/healthz`.

### Release flow

Version bumps land in the PR that ships the change. Releases are cut on
`main` after merge: tag `vX.Y.Z` and push the tag. Releases deploy the
service; there is no PyPI publish step.

## Tests

```bash
make test           # mocked suite; no server needed
```

With the server running (`make serve` in another terminal):

```bash
make smoke          # live checks: healthz 200, no-key 401, scaffolds 501, bad body 422
make webapp         # test web app on http://localhost:3000
```

The test web app (`webapp/`, plain HTML + JS, no build step) calls every
endpoint from the browser and renders status and body. During bootstrap the
expected results are 200 for `/healthz` and 501 everywhere else. The service
allows the web app's origin through CORS by default; deployments set
`HTTP_CORS_ORIGINS` to their real web app origins.

The harness runs **with** authentication rather than disabling it, so local
runs exercise the same path a deployment does. `make serve` and `make all` use
the key `local-dev-key` (override with `DEV_API_KEY=...`), and `make all`
links the web app with that key prefilled. The key field in the page stays
editable, so a wrong key can be tried against the real 401.

## Development conventions

- Never commit directly to `main`; branch from the updated remote first.
- Run tests before any commit; lint and format with
  `poetry run ruff check .` and `poetry run black .`.
- `.env` and credential files are gitignored; `env_template` is the only
  committed configuration file and must stay in sync with this README.
