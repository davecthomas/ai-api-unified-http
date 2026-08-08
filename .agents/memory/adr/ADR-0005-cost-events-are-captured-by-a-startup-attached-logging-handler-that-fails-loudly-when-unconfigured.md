# ADR-0005 Cost events are captured by a startup-attached logging handler that fails loudly when unconfigured

Status: accepted
Date: 2026-08-08
Owners: 2355287-davecthomas
Must read: true
Supersedes: 
Superseded by: 
ai-generated: True
ai-model: claude-opus-5
ai-tool: claude
ai-surface: claude-code
ai-executor: local-agent

Purpose: Cost events are captured by a startup-attached logging handler that fails loudly when unconfigured
Derived from: [2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b6](../daily/2026-08-08/events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b6.md)

## Context

- The library already emits one structured record per call carrying the cost attribution, so the service neither computes nor duplicates cost accounting. What the service must guarantee is that those records reach somewhere durable — a cost event silently dropped is spend with no record of who incurred it.

## Decision

- Cost capture is a logging-handler concern, not a code path through request handling. The library emits an `ai_api_call_cost` event on the logger named by its `emit_cost_topic` setting (default `ai_api_unified.observability.cost`); the service attaches a handler to that topic at application startup.
- A startup check fails loudly when the topic has no handler, so a misconfigured deployment refuses to start rather than serving traffic that quietly loses cost events.
- Bootstrap ships a JSON-lines file handler; the destination is expected to become a metrics pipeline without changing the capture mechanism.
- Streaming calls emit their cost event when the stream ends, including client-abandoned streams, because the library handles `GeneratorExit`.

## Consequences

- Promote to an ADR covering cost-event capture; swapping the file handler for a metrics pipeline must keep the fail-loud startup check covering the new destination.

## Source memory events

- [2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b6](../daily/2026-08-08/events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b6.md)

## Related code paths

- docs/requirements.md
- docs/technical-design.md
