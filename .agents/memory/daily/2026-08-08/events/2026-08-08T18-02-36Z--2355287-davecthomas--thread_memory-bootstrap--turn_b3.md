---
agentmemory_version: "0.4.4"
timestamp: "2026-08-08T18:02:36Z"
author: "2355287-davecthomas"
branch: "feat/local-dev-harness"
thread_id: "memory-bootstrap"
turn_id: "b3"
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
  - "env_template"
design_docs_touched:
  - "docs/requirements.md"
diff_summary: "bootstrap-derived decision candidate; no working-tree diff"
verification:
  - "docs/requirements.md Hard requirements 4-6 define the credential boundary, env-var parity, and no-committed-config rules"
  - "env_template is committed while .env is gitignored, matching requirement 6"
---

## Why

- Web apps must be able to call AI providers without ever holding provider credentials, and operators must be able to reuse a working library .env unchanged, or configuration will fork between the library and the service.

## What changed

- Provider keys (OpenAI/Anthropic/Google, ...) live only in the service environment. Web apps authenticate to the service and never hold provider credentials.
- Environment variable names match the library's (COMPLETIONS_ENGINE, COMPLETIONS_MODEL_NAME, ANTHROPIC_API_KEY, ...) so a working library .env works here unchanged.
- Configuration is never committed: .env is gitignored; the committed env_template documents every variable.
- v1 bootstrap ships without an auth scheme, but an auth layer is required before any non-local deployment and is the first post-bootstrap task.

## Evidence

- docs/requirements.md, "Hard requirements" items 4, 5, 6 and "Out of scope for v1" (authentication scheme).
- docs/technical-design.md, "Future work" (auth layer before any non-local deployment).
- Commit b34dedb (2026-08-08) added env_template alongside the scaffold.

## Next

- Promote to an ADR covering the credential boundary and configuration parity rules.
