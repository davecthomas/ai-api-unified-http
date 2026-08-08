---
agentmemory_version: "0.4.4"
timestamp: "2026-08-08T18:02:36Z"
author: "2355287-davecthomas"
branch: "feat/local-dev-harness"
thread_id: "memory-bootstrap"
turn_id: "b6"
workstream_id: "memory-bootstrap"
workstream_scope: "bootstrap foundational decisions from design docs and commit history"
decision_candidate: true
ai_generated: true
ai_model: "claude-opus-5"
ai_tool: "claude"
ai_surface: "claude-code"
ai_executor: "local-agent"
bootstrapped_at: "2026-08-08T18:26:00Z"
related_adrs: []
files_touched:
  - "docs/requirements.md"
  - "docs/technical-design.md"
design_docs_touched:
  - "docs/requirements.md"
  - "docs/technical-design.md"
diff_summary: "bootstrap-derived decision candidate; no working-tree diff"
verification:
  - "docs/requirements.md Hard requirement 3 requires cost events reach a durable, queryable destination"
  - "docs/technical-design.md Cost-event capture section names the emit_cost_topic logger and the startup handler attachment"
  - "docs/technical-design.md Risks table pairs lost cost events with a fail-loud startup check"
---

## Why

- The library already emits one structured record per call carrying the cost attribution, so the service neither computes nor duplicates cost accounting. What the service must guarantee is that those records reach somewhere durable — a cost event silently dropped is spend with no record of who incurred it.

## What changed

- Cost capture is a logging-handler concern, not a code path through request handling. The library emits an `ai_api_call_cost` event on the logger named by its `emit_cost_topic` setting (default `ai_api_unified.observability.cost`); the service attaches a handler to that topic at application startup.
- A startup check fails loudly when the topic has no handler, so a misconfigured deployment refuses to start rather than serving traffic that quietly loses cost events.
- Bootstrap ships a JSON-lines file handler; the destination is expected to become a metrics pipeline without changing the capture mechanism.
- Streaming calls emit their cost event when the stream ends, including client-abandoned streams, because the library handles `GeneratorExit`.

## Evidence

- docs/requirements.md, "Hard requirements" item 3 (cost events reach a durable destination).
- docs/technical-design.md, "Cost-event capture" and "Risks" (cost events lost if the handler is misconfigured).
- Commit b34dedb (2026-08-08) established the startup path this handler attaches to.

## Next

- Promote to an ADR covering cost-event capture; swapping the file handler for a metrics pipeline must keep the fail-loud startup check covering the new destination.
