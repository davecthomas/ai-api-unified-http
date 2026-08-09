# Local development harness for ai-api-unified-http.
# `make help` lists targets. The service runs on $(PORT).
#
# The browser console lives in its own repo:
#   https://github.com/davecthomas/ai-api-unified-http-webapp

PORT ?= 8080
# Local development key. The harness runs WITH authentication rather than
# disabling it, so local runs exercise the same path a deployment does. Real
# deployments set HTTP_API_KEYS to a generated secret in their own environment.
DEV_API_KEY ?= local-dev-key
DEV_ENV = HTTP_API_KEYS="local:$(DEV_API_KEY)"

# Sibling checkouts to copy a working .env from, in preference order.
ENV_SOURCES = ../ai_api_unified/.env ../sample_ai_api_unified/.env

.PHONY: help install lint test serve smoke env

help:
	@echo "env          copy a working .env from a sibling ai_api_unified checkout"
	@echo "install      poetry install with dev extras"
	@echo "lint         ruff + black --check"
	@echo "test         mocked test suite (no server needed)"
	@echo "serve        run the service on http://localhost:$(PORT) (reload on edit)"
	@echo "             local API key: $(DEV_API_KEY)  (override with DEV_API_KEY=...)"
	@echo "smoke        live checks against a running server on port $(PORT)"

# Copy a working .env from a sibling checkout. Provider variable names are
# identical to the library's, so a .env that works there works here unchanged.
# Refuses to clobber an existing .env, since it holds real keys.
env:
	@if [ -f .env ]; then \
		echo ".env already exists — not overwriting. Remove it first to re-copy."; \
		exit 1; \
	fi; \
	for candidate in $(ENV_SOURCES); do \
		if [ -f "$$candidate" ]; then \
			cp "$$candidate" .env; \
			echo "copied $$candidate -> .env"; \
			echo "review it: this service needs no IMAGE_/VIDEO_ settings, and"; \
			echo "COMPLETIONS_MODEL_NAME must match COMPLETIONS_ENGINE."; \
			exit 0; \
		fi; \
	done; \
	echo "no sibling .env found in: $(ENV_SOURCES)"; \
	echo "fall back to: cp env_template .env"; \
	exit 1

install:
	poetry install --extras dev

lint:
	poetry run ruff check .
	poetry run black --check .

test:
	poetry run pytest -q

serve:
	$(DEV_ENV) poetry run uvicorn ai_api_unified_http.app:create_app --factory --reload --port $(PORT)

# Live smoke against a running server. Every v1 endpoint is live, so these
# reach real providers and cost real money — keep the prompts tiny.
# Fails fast with a hint when the server is not up.
smoke:
	@curl -sf http://localhost:$(PORT)/healthz > /dev/null \
		|| (echo "server not reachable on port $(PORT) — run 'make serve' first" && exit 1)
	@echo "healthz:      $$(curl -s http://localhost:$(PORT)/healthz)"
	@echo "no key:       HTTP $$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:$(PORT)/v1/completions -H 'content-type: application/json' -d '{"engine":"claude","prompt":"hi"}') (expect 401)"
	@echo "completions:  HTTP $$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:$(PORT)/v1/completions -H "authorization: Bearer $(DEV_API_KEY)" -H 'content-type: application/json' -d '{"engine":"claude","model":"claude-haiku-4-5","prompt":"Say OK","max_response_tokens":16}') (expect 200)"
	@echo "models:       HTTP $$(curl -s -o /dev/null -w '%{http_code}' -H "authorization: Bearer $(DEV_API_KEY)" "http://localhost:$(PORT)/v1/models?engine=claude") (expect 200)"
	@echo "stream:       HTTP $$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:$(PORT)/v1/completions -H "authorization: Bearer $(DEV_API_KEY)" -H 'content-type: application/json' -d '{"engine":"claude","model":"claude-haiku-4-5","prompt":"hi","stream":true}') (expect 200)"
	@echo "bad body:     HTTP $$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:$(PORT)/v1/completions -H "authorization: Bearer $(DEV_API_KEY)" -H 'content-type: application/json' -d '{}') (expect 422)"
	@echo "tokens:       HTTP $$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:$(PORT)/v1/tokens/count -H "authorization: Bearer $(DEV_API_KEY)" -H 'content-type: application/json' -d '{"engine":"claude","model":"claude-haiku-4-5","prompt":"hi"}') (expect 200)"
	@echo "smoke OK"
