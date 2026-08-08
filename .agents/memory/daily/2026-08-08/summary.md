# 2026-08-08 summary

## Snapshot

- Captured 6 memory events.
- Main work: The service pins a published PyPI release of ai-api-unified (2.22.0 at bootstrap) and consumes only its public API. The library does not change to support this service.
- Top decision: The service must never fork or pressure the library's design: one library implementation is the whole point of the HTTP adapter. Letting the service drive library changes, or adding overlapping behavior like retries, would create drift and double-retry latency amplification. ([2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b2.md))
- Blockers: docs/technical-design.md, "Risks" (no service retry layer) and "Future work" (upstream improvements are non-blockers).

| Metric | Value |
|---|---|
| Memory events captured | 6 |
| Repo files changed | 6 |
| Decision candidates | 6 |
| Active blockers | 1 |

## Major work completed

- The service pins a published PyPI release of ai-api-unified (2.22.0 at bootstrap) and consumes only its public API. The library does not change to support this service.
- Provider keys (OpenAI/Anthropic/Google, ...) live only in the service environment. Web apps authenticate to the service and never hold provider credentials.
- The service keeps a process-wide dict of engine clients keyed by (engine, model), built on first use and reused across requests.
- Conversation turns are stateless: callers send full history each turn and execute tools themselves. AITurnResult.raw_content is serialized into an opaque string token; clients store and echo it untouched and must never parse it. The token's encoding is a service implementation detail versioned alongside the service.
- Cost capture is a logging-handler concern, not a code path through request handling. The library emits an `ai_api_call_cost` event on the logger named by its `emit_cost_topic` setting (default `ai_api_unified.observability.cost`); the service attaches a handler to that topic at application startup.
- Streaming completions bridge the library's sync generator to SSE through a `StreamingResponse` running in the threadpool. Async streaming is future library work, not a prerequisite.

## Why this mattered

- The service must never fork or pressure the library's design: one library implementation is the whole point of the HTTP adapter. Letting the service drive library changes, or adding overlapping behavior like retries, would create drift and double-retry latency amplification.
- Web apps must be able to call AI providers without ever holding provider credentials, and operators must be able to reuse a working library .env unchanged, or configuration will fork between the library and the service.
- AIFactory.get_* constructs a new provider client per call, and each construction re-reads .env, re-parses the middleware YAML, and (Gemini only) makes a models.get network round trip. Per-request construction is wasteful and, for Gemini, adds a network call to every request.
- The service sits between clients and the library across version boundaries in three places: conversation state echoed back by clients, the HTTP API shape, and the pinned library. Each needs an explicit contract or clients will couple to implementation details.
- The library already emits one structured record per call carrying the cost attribution, so the service neither computes nor duplicates cost accounting. What the service must guarantee is that those records reach somewhere durable — a cost event silently dropped is spend with no record of who incurred it.
- The library's streaming surface is a sync generator, and it raises when streaming and PII redaction are both enabled. Those constraints are inherited rather than chosen, and they reach all the way out to what a caller can expect from the streaming endpoint, so they belong in the service's own contract rather than buried as an implementation note.

## Active blockers

- docs/technical-design.md, "Risks" (no service retry layer) and "Future work" (upstream improvements are non-blockers).

## Decision candidates

- The service must never fork or pressure the library's design: one library implementation is the whole point of the HTTP adapter. Letting the service drive library changes, or adding overlapping behavior like retries, would create drift and double-retry latency amplification. ([2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b2.md))
- Web apps must be able to call AI providers without ever holding provider credentials, and operators must be able to reuse a working library .env unchanged, or configuration will fork between the library and the service. ([2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b3.md))
- AIFactory.get_* constructs a new provider client per call, and each construction re-reads .env, re-parses the middleware YAML, and (Gemini only) makes a models.get network round trip. Per-request construction is wasteful and, for Gemini, adds a network call to every request. ([2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b4.md))
- The service sits between clients and the library across version boundaries in three places: conversation state echoed back by clients, the HTTP API shape, and the pinned library. Each needs an explicit contract or clients will couple to implementation details. ([2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b5.md))
- The library already emits one structured record per call carrying the cost attribution, so the service neither computes nor duplicates cost accounting. What the service must guarantee is that those records reach somewhere durable — a cost event silently dropped is spend with no record of who incurred it. ([2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b6.md))
- The library's streaming surface is a sync generator, and it raises when streaming and PII redaction are both enabled. Those constraints are inherited rather than chosen, and they reach all the way out to what a caller can expect from the streaming endpoint, so they belong in the service's own contract rather than buried as an implementation note. ([2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b7.md))

## Next likely steps

- Streaming ships after the buffered `POST /v1/completions` path; the concurrency ceiling becomes a deployment sizing input at that point.

## Relevant event shards

- [2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b2.md)
- [2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b3.md)
- [2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b4.md)
- [2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b5.md)
- [2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b6.md)
- [2026-08-08 18:02:36 UTC by 2355287-davecthomas](events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b7.md)
