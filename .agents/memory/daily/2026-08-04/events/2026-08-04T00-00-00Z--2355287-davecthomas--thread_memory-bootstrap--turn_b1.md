---
agentmemory_version: "0.4.4"
timestamp: "2026-08-04T00:00:00Z"
author: "2355287-davecthomas"
branch: "feat/local-dev-harness"
thread_id: "memory-bootstrap"
turn_id: "b1"
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
  - "docs/requirements.md"
  - "docs/technical-design.md"
design_docs_touched:
  - "docs/requirements.md"
  - "docs/technical-design.md"
diff_summary: "bootstrap-derived decision candidate; no working-tree diff"
verification:
  - "docs/requirements.md sections Problem and Decision state the decision and record its date as 2026-08-04"
  - "docs/technical-design.md Architecture diagram shows web app -> generated client -> FastAPI service -> ai-api-unified library"
---

## Why

- Non-Python web applications need ai-api-unified's provider abstraction, pricing registry, model lifecycle enforcement, cost attribution, and PII/observability middleware. Porting the library to TypeScript would duplicate logic and let the two copies drift.

## What changed

- Decided to run the Python ai-api-unified library behind a thin HTTP service (this repo) so one implementation serves every consumer.
- TypeScript clients are generated from the service's OpenAPI spec; hand-written clients are out of scope.
- The service is deliberately a thin adapter: request validation, client pooling, SSE bridging, and cost-event capture. All provider logic, pricing, lifecycle enforcement, and middleware stay in the library.

## Evidence

- docs/requirements.md, "Problem" and "Decision" sections (decision record dated 2026-08-04).
- docs/technical-design.md, "Architecture" section and diagram.
- Commit b34dedb (2026-08-08) bootstrapped the FastAPI scaffold with the v1 API surface implementing this decision.

## Next

- Promote to an ADR as the repo's founding architecture decision.
