# Local development harness for ai-api-unified-http.
# `make help` lists targets. Ports: server on $(PORT), web app on $(WEBAPP_PORT).

PORT ?= 8080
WEBAPP_PORT ?= 3000

.PHONY: help install lint test serve smoke webapp

help:
	@echo "install      poetry install with dev extras"
	@echo "lint         ruff + black --check"
	@echo "test         mocked test suite (no server needed)"
	@echo "serve        run the service on http://localhost:$(PORT) (reload on edit)"
	@echo "smoke        live checks against a running server on port $(PORT)"
	@echo "webapp       serve the test web app on http://localhost:$(WEBAPP_PORT)"

install:
	poetry install --extras dev

lint:
	poetry run ruff check .
	poetry run black --check .

test:
	poetry run pytest -q

serve:
	poetry run uvicorn ai_api_unified_http.app:create_app --factory --reload --port $(PORT)

# Live smoke: healthz must be 200; scaffolded endpoints must answer 501.
# Fails fast with a hint when the server is not up.
smoke:
	@curl -sf http://localhost:$(PORT)/healthz > /dev/null \
		|| (echo "server not reachable on port $(PORT) — run 'make serve' first" && exit 1)
	@echo "healthz:      $$(curl -s http://localhost:$(PORT)/healthz)"
	@echo "completions:  HTTP $$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:$(PORT)/v1/completions -H 'content-type: application/json' -d '{"engine":"claude","prompt":"hi"}') (expect 501)"
	@echo "models:       HTTP $$(curl -s -o /dev/null -w '%{http_code}' http://localhost:$(PORT)/v1/models) (expect 501)"
	@echo "bad body:     HTTP $$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:$(PORT)/v1/completions -H 'content-type: application/json' -d '{}') (expect 422)"
	@echo "smoke OK"

webapp:
	@echo "web app on http://localhost:$(WEBAPP_PORT) — calls the API on port 8080 by default"
	@echo "for a non-default API port: http://localhost:$(WEBAPP_PORT)/?base=http://localhost:$(PORT)"
	cd webapp && python3 -m http.server $(WEBAPP_PORT)
