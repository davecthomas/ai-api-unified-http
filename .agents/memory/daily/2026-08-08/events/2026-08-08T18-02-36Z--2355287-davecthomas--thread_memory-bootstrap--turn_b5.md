---
agentmemory_version: "0.4.4"
timestamp: "2026-08-08T18:02:36Z"
author: "2355287-davecthomas"
branch: "feat/local-dev-harness"
thread_id: "memory-bootstrap"
turn_id: "b5"
workstream_id: "memory-bootstrap"
workstream_scope: "bootstrap foundational decisions from design docs and commit history"
decision_candidate: true
ai_generated: true
ai_model: "claude-fable-5"
ai_tool: "claude"
ai_surface: "claude-code"
ai_executor: "local-agent"
bootstrapped_at: "2026-08-08T18:21:31Z"
related_adrs: []
files_touched:
  - "docs/technical-design.md"
  - "docs/requirements.md"
design_docs_touched:
  - "docs/technical-design.md"
  - "docs/requirements.md"
diff_summary: "bootstrap-derived decision candidate; no working-tree diff"
verification:
  - "docs/technical-design.md Conversation raw_content round-trip section defines the opaque token contract"
  - "docs/technical-design.md API versioning section defines the three independent version identities reported by /healthz"
---

## Why

- The service sits between clients and the library across version boundaries in three places: conversation state echoed back by clients, the HTTP API shape, and the pinned library. Each needs an explicit contract or clients will couple to implementation details.

## What changed

- Conversation turns are stateless: callers send full history each turn and execute tools themselves. AITurnResult.raw_content is serialized into an opaque string token; clients store and echo it untouched and must never parse it. The token's encoding is a service implementation detail versioned alongside the service.
- API versioning uses three independent identities: URI major version (/v1/) bumps only on breaking request/response shape changes (side-by-side /v1 and /v2 allowed during migration); the service semver moves with ordinary releases; the pinned library version is independent of both. /healthz reports all of them so operators can correlate behavior with releases.

## Evidence

- docs/technical-design.md, "Conversation raw_content round-trip", "API versioning", and "Endpoint-to-library mapping" (conversations/turn stateless, per library ADR-0018) sections.
- docs/requirements.md, v1 scope table (versioned /v1 endpoint surface).
- Commit b34dedb (2026-08-08) established the /v1 URI surface and /healthz version reporting.

## Next

- Candidate for ADR promotion covering the client-facing compatibility contracts.
