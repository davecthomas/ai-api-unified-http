# ai-api-unified-http 0.2.1

HTTP interface to the [ai-api-unified](https://github.com/davecthomas/ai-api-unified)
Python library, for web apps and other non-Python consumers. One
implementation of provider logic, pricing, model lifecycle, and middleware —
exposed over REST, with TypeScript clients generated from the OpenAPI spec.

Status: **bootstrap**. The server runs and serves its API surface, but every
model-invoking endpoint returns `501 Not Implemented`. `GET /healthz` is live.
See [docs/requirements.md](docs/requirements.md) for scope and
[docs/technical-design.md](docs/technical-design.md) for the architecture and
the endpoint-to-library mapping.

## Setup

Requires Python `>=3.11,<3.14` and [Poetry](https://python-poetry.org/).

```bash
poetry install --extras dev

# Configuration: copy the template, then fill in only the providers you use.
# .env is gitignored — real keys never get committed.
cp env_template .env
```

Variable names in `.env` are identical to the ai-api-unified library's, so a
`.env` that works for the library works here unchanged. See the comments in
[`env_template`](env_template) for every supported variable.

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
| POST | `/v1/completions` | Text completion; `"stream": true` for SSE | 501 |
| POST | `/v1/structured` | Schema-validated structured output | 501 |
| POST | `/v1/conversations/turn` | One stateless tool-capable conversation turn | 501 |
| POST | `/v1/embeddings` | Embedding vectors | 501 |
| POST | `/v1/tokens/count` | Provider-side token count | 501 |
| GET | `/v1/models` | Model catalog: capabilities, pricing, lifecycle | 501 |
| GET | `/healthz` | Liveness and versions | live |

## Versioning

Two independent version tracks:

- **API version** — the `/v1/` URI prefix. Bumps only on breaking changes to
  request or response shapes; `/v1` and `/v2` can run side by side during a
  migration.
- **Service version** — semantic versioning, currently `0.1.0`.
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
make smoke          # live checks: healthz 200, scaffolds 501, bad body 422
make webapp         # test web app on http://localhost:3000
```

The test web app (`webapp/`, plain HTML + JS, no build step) calls every
endpoint from the browser and renders status and body. During bootstrap the
expected results are 200 for `/healthz` and 501 everywhere else. The service
allows the web app's origin through CORS by default; deployments set
`HTTP_CORS_ORIGINS` to their real web app origins.

## Development conventions

- Never commit directly to `main`; branch from the updated remote first.
- Run tests before any commit; lint and format with
  `poetry run ruff check .` and `poetry run black .`.
- `.env` and credential files are gitignored; `env_template` is the only
  committed configuration file and must stay in sync with this README.
