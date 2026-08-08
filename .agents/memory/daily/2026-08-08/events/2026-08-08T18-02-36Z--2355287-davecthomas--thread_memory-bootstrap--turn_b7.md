---
agentmemory_version: "0.4.4"
timestamp: "2026-08-08T18:02:36Z"
author: "2355287-davecthomas"
branch: "feat/local-dev-harness"
thread_id: "memory-bootstrap"
turn_id: "b7"
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
  - "docs/technical-design.md Endpoint-to-library mapping bridges send_prompt_streaming to SSE via StreamingResponse in the threadpool"
  - "docs/requirements.md Constraints inherited from the library states streaming and PII redaction are mutually exclusive"
  - "docs/technical-design.md Risks table sizes the SSE concurrency ceiling at threadpool size x workers"
---

## Why

- The library's streaming surface is a sync generator, and it raises when streaming and PII redaction are both enabled. Those constraints are inherited rather than chosen, and they reach all the way out to what a caller can expect from the streaming endpoint, so they belong in the service's own contract rather than buried as an implementation note.

## What changed

- Streaming completions bridge the library's sync generator to SSE through a `StreamingResponse` running in the threadpool. Async streaming is future library work, not a prerequisite.
- The streaming endpoint is unredacted, because streaming and PII redaction are mutually exclusive in the library. Callers needing redaction use the buffered endpoint. This is a caller-visible property of the contract, not an implementation detail.
- Each active stream occupies one threadpool thread for its lifetime, so streaming concurrency is capped at threadpool size times worker count, and deployment sizes those numbers deliberately.

## Evidence

- docs/requirements.md, "Constraints inherited from the library" (streaming/redaction exclusivity, Bedrock has no async, SSE concurrency ceiling).
- docs/technical-design.md, "Endpoint-to-library mapping" (`send_prompt_streaming` bridged to SSE, no redaction, no retries on streams).
- docs/technical-design.md, "Risks" (one thread per active stream).

## Next

- Streaming ships after the buffered `POST /v1/completions` path; the concurrency ceiling becomes a deployment sizing input at that point.
