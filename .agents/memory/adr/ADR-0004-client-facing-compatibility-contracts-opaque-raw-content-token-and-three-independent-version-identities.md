# ADR-0004 Client-facing compatibility contracts: opaque raw_content token and three independent version identities

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

Purpose: Client-facing compatibility contracts: opaque raw_content token and three independent version identities
Derived from: [2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b5](../daily/2026-08-08/events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b5.md)

## Context

- The service sits between clients and the library across version boundaries in three places: conversation state echoed back by clients, the HTTP API shape, and the pinned library. Each needs an explicit contract or clients will couple to implementation details.

## Decision

- Conversation turns are stateless: callers send full history each turn and execute tools themselves. AITurnResult.raw_content is serialized into an opaque string token; clients store and echo it untouched and must never parse it. The token's encoding is a service implementation detail versioned alongside the service.
- API versioning uses three independent identities: URI major version (/v1/) bumps only on breaking request/response shape changes (side-by-side /v1 and /v2 allowed during migration); the service semver moves with ordinary releases; the pinned library version is independent of both. /healthz reports all of them so operators can correlate behavior with releases.

## Consequences

- Candidate for ADR promotion covering the client-facing compatibility contracts.

## Source memory events

- [2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b5](../daily/2026-08-08/events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b5.md)

## Related code paths

- docs/technical-design.md
- docs/requirements.md
