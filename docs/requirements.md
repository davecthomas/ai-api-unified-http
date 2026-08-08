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
   published PyPI release (`ai-api-unified==2.22.0` at bootstrap) and consumes
   only its public API. Library improvements that would simplify the service
   (async streaming, a pluggable cost sink) are tracked as future library work,
   not prerequisites.
2. **Middleware is preserved.** Every call through the service passes through
   the library's PII redaction and observability middleware, configured by one
   middleware YAML owned by the service deployment.
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

| Endpoint | Status at bootstrap |
|---|---|
| `POST /v1/completions` (buffered and SSE streaming) | 501 scaffold |
| `POST /v1/structured` | 501 scaffold |
| `POST /v1/conversations/turn` | 501 scaffold |
| `POST /v1/embeddings` | 501 scaffold |
| `POST /v1/tokens/count` | 501 scaffold |
| `GET /v1/models` (capabilities + pricing + lifecycle) | 501 scaffold |
| `GET /healthz` | live |

## Out of scope for v1

- **Video and batch completions** — both are long-running job workflows (the
  library's blocking helpers wait up to 900 s and 24 h respectively). They
  need submit/poll job resources and artifact storage (object store + signed
  URLs). Deferred until v1 is proven.
- **Images and voice** — return bytes/files; same storage decision as video.
- **Authentication scheme** — v1 bootstrap ships without auth; an internal
  API-key or org-standard auth layer is required before any non-local
  deployment. Tracked as the first post-bootstrap task.

## Constraints inherited from the library

- Streaming and PII redaction are mutually exclusive: the library raises when
  both are enabled. The streaming endpoint is therefore unredacted; callers
  needing redaction use the buffered endpoint.
- Bedrock engines have no async support and no per-call timeout.
- Each active SSE stream occupies one threadpool thread until the library
  gains async streaming; concurrency ceiling = threadpool size x workers.
