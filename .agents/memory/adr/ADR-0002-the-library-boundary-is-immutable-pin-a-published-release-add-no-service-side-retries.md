# ADR-0002 The library boundary is immutable: pin a published release, add no service-side retries

Status: accepted
Date: 2026-08-08
Owners: 2355287-davecthomas
Must read: true
Supersedes: 
Superseded by: 
ai-generated: True
ai-model: claude-fable-5
ai-tool: claude
ai-surface: claude-code
ai-executor: local-agent

Purpose: The library boundary is immutable: pin a published release, add no service-side retries
Derived from: [2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b2](../daily/2026-08-08/events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b2.md)

## Context

- The service must never fork or pressure the library's design: one library implementation is the whole point of the HTTP adapter. Letting the service drive library changes, or adding overlapping behavior like retries, would create drift and double-retry latency amplification.

## Decision

- The service pins a published PyPI release of ai-api-unified (2.22.0 at bootstrap) and consumes only its public API. The library does not change to support this service.
- Library improvements that would simplify the service (async streaming, pluggable cost sink, cached config loading) are tracked as future library work, never prerequisites.
- The service adds no retry layer; retries stay library/SDK-owned.
- Library-inherited constraints are accepted as service constraints: streaming and PII redaction are mutually exclusive (streaming endpoint is unredacted), Bedrock engines have no async support or per-call timeout, and each active SSE stream occupies one threadpool thread.

## Consequences

- Promote to an ADR covering the library-boundary contract.

## Source memory events

- [2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b2](../daily/2026-08-08/events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b2.md)

## Related code paths

- docs/requirements.md
- docs/technical-design.md
- pyproject.toml
