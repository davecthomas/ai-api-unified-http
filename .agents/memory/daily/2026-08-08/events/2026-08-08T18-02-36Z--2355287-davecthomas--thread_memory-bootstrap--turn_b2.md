---
agentmemory_version: "0.4.4"
timestamp: "2026-08-08T18:02:36Z"
author: "2355287-davecthomas"
branch: "feat/local-dev-harness"
thread_id: "memory-bootstrap"
turn_id: "b2"
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
  - "pyproject.toml"
design_docs_touched:
  - "docs/requirements.md"
  - "docs/technical-design.md"
diff_summary: "bootstrap-derived decision candidate; no working-tree diff"
verification:
  - "docs/requirements.md Hard requirement 1 names the pinned release ai-api-unified==2.22.0 and forbids library changes for the service"
  - "docs/technical-design.md Risks table states the service adds no retry layer; retries stay library/SDK-owned"
---

## Why

- The service must never fork or pressure the library's design: one library implementation is the whole point of the HTTP adapter. Letting the service drive library changes, or adding overlapping behavior like retries, would create drift and double-retry latency amplification.

## What changed

- The service pins a published PyPI release of ai-api-unified (2.22.0 at bootstrap) and consumes only its public API. The library does not change to support this service.
- Library improvements that would simplify the service (async streaming, pluggable cost sink, cached config loading) are tracked as future library work, never prerequisites.
- The service adds no retry layer; retries stay library/SDK-owned.
- Library-inherited constraints are accepted as service constraints: streaming and PII redaction are mutually exclusive (streaming endpoint is unredacted), Bedrock engines have no async support or per-call timeout, and each active SSE stream occupies one threadpool thread.

## Evidence

- docs/requirements.md, "Hard requirements" item 1 and "Constraints inherited from the library" section.
- docs/technical-design.md, "Risks" (no service retry layer) and "Future work" (upstream improvements are non-blockers).
- Commit b34dedb (2026-08-08) pins the library in the scaffold.

## Next

- Promote to an ADR covering the library-boundary contract.
