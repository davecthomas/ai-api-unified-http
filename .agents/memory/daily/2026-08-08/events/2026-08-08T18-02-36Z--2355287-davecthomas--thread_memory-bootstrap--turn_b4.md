---
agentmemory_version: "0.4.4"
timestamp: "2026-08-08T18:02:36Z"
author: "2355287-davecthomas"
branch: "feat/local-dev-harness"
thread_id: "memory-bootstrap"
turn_id: "b4"
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
design_docs_touched:
  - "docs/technical-design.md"
diff_summary: "bootstrap-derived decision candidate; no working-tree diff"
verification:
  - "docs/technical-design.md Client pool section documents the (engine, model) keyed pool and the thread-safety verification behind it"
---

## Why

- AIFactory.get_* constructs a new provider client per call, and each construction re-reads .env, re-parses the middleware YAML, and (Gemini only) makes a models.get network round trip. Per-request construction is wasteful and, for Gemini, adds a network call to every request.

## What changed

- The service keeps a process-wide dict of engine clients keyed by (engine, model), built on first use and reused across requests.
- Safety was verified before adopting the pool: engine instances hold no per-conversation state, provider SDK clients are thread-safe, and the library's mutable instance attributes are idempotent lazy caches.
- Accepted consequence: client-construction failures surface at first use per (engine, model), not per request.

## Evidence

- docs/technical-design.md, "Client pool" and "Risks" (Gemini construction network call) sections.
- Commit b34dedb (2026-08-08) bootstrapped the service scaffold this pooling design belongs to.

## Next

- Candidate for ADR promotion if pooling invariants need to bind future contributors; otherwise remains a recorded design decision.
