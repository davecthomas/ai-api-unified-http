# ADR-0001 Serve ai-api-unified through a thin HTTP service with generated TypeScript clients

Status: accepted
Date: 2026-08-04
Owners: 2355287-davecthomas
Must read: true
Supersedes: 
Superseded by: 
ai-generated: True
ai-model: claude-fable-5
ai-tool: claude
ai-surface: claude-code
ai-executor: local-agent

Purpose: Serve ai-api-unified through a thin HTTP service with generated TypeScript clients
Derived from: [2026-08-04T00-00-00Z--2355287-davecthomas--thread_memory-bootstrap--turn_b1](../daily/2026-08-04/events/2026-08-04T00-00-00Z--2355287-davecthomas--thread_memory-bootstrap--turn_b1.md)

## Context

- Non-Python web applications need ai-api-unified's provider abstraction, pricing registry, model lifecycle enforcement, cost attribution, and PII/observability middleware. Porting the library to TypeScript would duplicate logic and let the two copies drift.

## Decision

- Decided to run the Python ai-api-unified library behind a thin HTTP service (this repo) so one implementation serves every consumer.
- TypeScript clients are generated from the service's OpenAPI spec; hand-written clients are out of scope.
- The service is deliberately a thin adapter: request validation, client pooling, SSE bridging, and cost-event capture. All provider logic, pricing, lifecycle enforcement, and middleware stay in the library.

## Consequences

- Promote to an ADR as the repo's founding architecture decision.

## Source memory events

- [2026-08-04T00-00-00Z--2355287-davecthomas--thread_memory-bootstrap--turn_b1](../daily/2026-08-04/events/2026-08-04T00-00-00Z--2355287-davecthomas--thread_memory-bootstrap--turn_b1.md)

## Related code paths

- docs/requirements.md
- docs/technical-design.md
