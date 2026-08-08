# ADR-0003 Provider credentials live only in the service environment with library env-var parity

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

Purpose: Provider credentials live only in the service environment with library env-var parity
Derived from: [2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b3](../daily/2026-08-08/events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b3.md)

## Context

- Web apps must be able to call AI providers without ever holding provider credentials, and operators must be able to reuse a working library .env unchanged, or configuration will fork between the library and the service.

## Decision

- Provider keys (OpenAI/Anthropic/Google, ...) live only in the service environment. Web apps authenticate to the service and never hold provider credentials.
- Environment variable names match the library's (COMPLETIONS_ENGINE, COMPLETIONS_MODEL_NAME, ANTHROPIC_API_KEY, ...) so a working library .env works here unchanged.
- Configuration is never committed: .env is gitignored; the committed env_template documents every variable.
- v1 bootstrap ships without an auth scheme, but an auth layer is required before any non-local deployment and is the first post-bootstrap task.

## Consequences

- Promote to an ADR covering the credential boundary and configuration parity rules.

## Source memory events

- [2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b3](../daily/2026-08-08/events/2026-08-08T18-02-36Z--2355287-davecthomas--thread_memory-bootstrap--turn_b3.md)

## Related code paths

- docs/requirements.md
- env_template
