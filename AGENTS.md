# Repository guidelines

ai-api-unified-http is an HTTP service over the ai-api-unified library. It
holds the provider API keys, issues run grants, and answers every model call a
meta-harness run makes. Change it as a service other programs depend on.

## Build and test

- Install: `poetry install --extras dev`.
- Test: `poetry run pytest -q`. Every test is mocked and makes no network
  call. A new test must stay that way.
- Lint and format: `poetry run ruff check .` and `poetry run black --check .`.
  Run `poetry run black .` to fix formatting.
- Do not run `make smoke`, `make smoke-cache` or any `make gcp-*` target. They
  call real providers, cost money, or change a Google Cloud project.

## Versioning

Every change under `src/` bumps the version by semver: patch for fixes and
internal changes, minor for new endpoints or fields, major for breaking
request, response, deploy or config changes. Docs-only and test-only changes
do not bump it.

The version lives in exactly three places, and they change together:

1. `pyproject.toml`: `version = "X.Y.Z"`
2. `src/ai_api_unified_http/__version__.py`: `__version__: str = "X.Y.Z"`
3. `README.md`: the title on line 1, `# ai-api-unified-http X.Y.Z`

`tests/test_version_sync.py` fails when they disagree. Do not tag or deploy:
an operator cuts releases.

## The API and its client

- The `/v1/` prefix changes only on a breaking request or response shape.
- A change to any request or response model changes `/openapi.json`. Run
  `make client` and commit `clients/typescript/openapi.json` and
  `clients/typescript/src/schema.ts` with the change. CI fails when the
  committed client is out of date.

## Configuration

- The library's configuration is `config/models.json`. Check an edit with
  `poetry run python -m ai_api_unified.config_schema validate config/models.json`.
- Keep the `observability` middleware's cost emission on. The service refuses
  to start when cost events would not be recorded.
- Secrets come from the environment, never from a file in the repo.

## Style

- US English in code, comments and docs.
- Type hints on every parameter, variable and return value.
- Update `README.md` when a change adds or alters an endpoint, a field or a
  setting.
