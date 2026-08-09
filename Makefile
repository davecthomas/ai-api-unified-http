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

.PHONY: help install lint test serve smoke env gcp-project gcp-secrets gcp-deploy gcp-url gcp-logs

help:
	@echo "env          copy a working .env from a sibling ai_api_unified checkout"
	@echo "install      poetry install with dev extras"
	@echo "lint         ruff + black --check"
	@echo "test         mocked test suite (no server needed)"
	@echo "serve        run the service on http://localhost:$(PORT) (reload on edit)"
	@echo "             local API key: $(DEV_API_KEY)  (override with DEV_API_KEY=...)"
	@echo "smoke        live checks against a running server on port $(PORT)"
	@echo ""
	@echo "  Google Cloud Run (all take PROJECT=<project-id>)"
	@echo "gcp-project  create the project, link BILLING=<account-id>, enable APIs"
	@echo "gcp-secrets  push provider keys from .env into Secret Manager"
	@echo "gcp-deploy   build with Cloud Build and deploy"
	@echo "gcp-url      print the deployed service URL"
	@echo "gcp-logs     tail recent service logs"

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


# --- Google Cloud Run -------------------------------------------------------
#
# Deploys into YOUR project with YOUR provider keys. Cloud Build compiles the
# image server-side, so no local Docker is needed.
#
# Secrets are mounted at runtime from Secret Manager rather than set as plain
# environment values, so a key never lands in the image or in the service's
# stored configuration.

REGION ?= us-central1
SERVICE ?= ai-api-unified-http
SECRET_KEYS = HTTP_API_KEYS ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_GEMINI_API_KEY

define require_project
	@test -n "$(PROJECT)" || (echo "set PROJECT=<project-id>" && exit 1)
endef

gcp-project:
	$(call require_project)
	@test -n "$(BILLING)" || (echo "set BILLING=<billing-account-id>; list them with: gcloud billing accounts list" && exit 1)
	gcloud projects create $(PROJECT) --name="$(PROJECT)" || echo "project exists, continuing"
	gcloud billing projects link $(PROJECT) --billing-account=$(BILLING)
	gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
		artifactregistry.googleapis.com secretmanager.googleapis.com --project=$(PROJECT)
	@echo "project ready: $(PROJECT)"

# Reads .env and writes each key it finds. A key already in Secret Manager gets
# a new version rather than an error, so re-running after a rotation is safe.
gcp-secrets:
	$(call require_project)
	@test -f .env || (echo "no .env — run 'make env' or copy env_template first" && exit 1)
	@set -a; . ./.env; set +a; \
	if [ -z "$$HTTP_API_KEYS" ]; then \
		HTTP_API_KEYS="webapp:$$(openssl rand -hex 32)"; \
		echo "HTTP_API_KEYS was unset; generated one. Read it back with:"; \
		echo "  gcloud secrets versions access latest --secret=HTTP_API_KEYS --project=$(PROJECT)"; \
	fi; \
	for name in $(SECRET_KEYS); do \
		value=$$(eval echo \$$$$name); \
		if [ -z "$$value" ]; then echo "  skip $$name (not set)"; continue; fi; \
		if gcloud secrets describe $$name --project=$(PROJECT) >/dev/null 2>&1; then \
			printf '%s' "$$value" | gcloud secrets versions add $$name --data-file=- --project=$(PROJECT) >/dev/null && echo "  updated $$name"; \
		else \
			printf '%s' "$$value" | gcloud secrets create $$name --data-file=- --replication-policy=automatic --project=$(PROJECT) >/dev/null && echo "  created $$name"; \
		fi; \
	done; \
	num=$$(gcloud projects describe $(PROJECT) --format="value(projectNumber)"); \
	for name in $(SECRET_KEYS); do \
		gcloud secrets add-iam-policy-binding $$name --project=$(PROJECT) \
			--member="serviceAccount:$$num-compute@developer.gserviceaccount.com" \
			--role="roles/secretmanager.secretAccessor" >/dev/null 2>&1 || true; \
	done; \
	echo "secrets ready in $(PROJECT)"

# One worker keeps the rate limit meaning what it says: the counter is
# per-process, so N workers would admit N times the configured limit.
gcp-deploy:
	$(call require_project)
	gcloud run deploy $(SERVICE) --project=$(PROJECT) --region=$(REGION) \
		--source . --allow-unauthenticated \
		--memory 512Mi --cpu 1 --concurrency 40 --max-instances 3 --timeout 3600 \
		--set-env-vars "COMPLETIONS_ENGINE=claude,HTTP_RATE_LIMIT=60,LOG_LEVEL=INFO,WEB_CONCURRENCY=1,HTTP_CORS_ORIGINS=$(CORS_ORIGINS)" \
		--set-secrets "HTTP_API_KEYS=HTTP_API_KEYS:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,OPENAI_API_KEY=OPENAI_API_KEY:latest,GOOGLE_GEMINI_API_KEY=GOOGLE_GEMINI_API_KEY:latest" \
		--quiet
	@$(MAKE) --no-print-directory gcp-url PROJECT=$(PROJECT)

CORS_ORIGINS ?= http://localhost:3000

gcp-url:
	$(call require_project)
	@echo "service:  $$(gcloud run services describe $(SERVICE) --project=$(PROJECT) --region=$(REGION) --format='value(status.url)')"
	@echo "api key:  gcloud secrets versions access latest --secret=HTTP_API_KEYS --project=$(PROJECT)"

gcp-logs:
	$(call require_project)
	gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="$(SERVICE)"' \
		--project=$(PROJECT) --limit 40 --freshness=30m --format="value(textPayload)"
