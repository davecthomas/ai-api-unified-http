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

.PHONY: help install lint test serve smoke env gcp-project gcp-secrets gcp-deploy gcp-url gcp-logs gcp-cicd gcp-artifacts client

help:
	@echo "env          copy a working .env from a sibling ai_api_unified checkout"
	@echo "install      poetry install with dev extras"
	@echo "lint         ruff + black --check"
	@echo "test         mocked test suite (no server needed)"
	@echo "serve        run the service on http://localhost:$(PORT) (reload on edit)"
	@echo "             local API key: $(DEV_API_KEY)  (override with DEV_API_KEY=...)"
	@echo "smoke        live checks against a running server on port $(PORT)"
	@echo "client       regenerate the TypeScript client from the OpenAPI spec"
	@echo ""
	@echo "  Google Cloud Run (all take PROJECT=<project-id>)"
	@echo "gcp-project  create the project, link BILLING=<account-id>, enable APIs"
	@echo "gcp-secrets  push provider keys from .env into Secret Manager"
	@echo "gcp-artifacts create the artifact bucket and its expiry rule (once)"
	@echo "gcp-deploy   build with Cloud Build and deploy"
	@echo "gcp-url      print the deployed service URL"
	@echo "gcp-logs     tail recent service logs"
	@echo "gcp-cicd     let GitHub Actions deploy without a stored credential (once)"

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


# Regenerate the TypeScript client. The spec comes from the app object rather
# than a running server, so this needs no port and no provider keys. CI runs
# the same steps and fails when the committed output has moved.
client:
	poetry run python scripts/dump_openapi.py clients/typescript/openapi.json
	cd clients/typescript && npm install --silent && npm run generate && npm run typecheck
	@echo "client regenerated; commit clients/typescript if it changed"

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
		--add-volume=name=artifacts,type=cloud-storage,bucket=$(BUCKET) \
		--add-volume-mount=volume=artifacts,mount-path=/artifacts \
		--no-cpu-throttling \
		--set-env-vars "COMPLETIONS_ENGINE=claude,HTTP_RATE_LIMIT=60,LOG_LEVEL=INFO,WEB_CONCURRENCY=1,HTTP_CLIENT_IP_FROM_XFF=1,HTTP_ARTIFACT_DIR=/artifacts,HTTP_CORS_ORIGINS=$(CORS_ORIGINS)" \
		--set-secrets "HTTP_API_KEYS=HTTP_API_KEYS:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,OPENAI_API_KEY=OPENAI_API_KEY:latest,GOOGLE_GEMINI_API_KEY=GOOGLE_GEMINI_API_KEY:latest" \
		--quiet
	@$(MAKE) --no-print-directory gcp-url PROJECT=$(PROJECT)

CORS_ORIGINS ?= http://localhost:3000

# Bucket for generated images and video, mounted into the service as a path.
#
# Generation is the expensive step, so an artifact has to outlive the request
# that made it: a dropped transfer is then a re-download rather than a
# re-generation. It cannot live in the container — Cloud Run's filesystem is
# in-memory, and with several instances a retry usually reaches one that never
# held the bytes.
#
# The lifecycle rule is the deletion mechanism. A service that scales to zero
# cannot be relied on to sweep, so the bucket does it.
BUCKET ?= $(PROJECT)-artifacts
ARTIFACT_TTL_DAYS ?= 1

gcp-artifacts:
	$(call require_project)
	@gcloud storage buckets create gs://$(BUCKET) --project=$(PROJECT) \
		--location=$(REGION) --uniform-bucket-level-access 2>/dev/null \
		|| echo "  bucket gs://$(BUCKET) exists"
	@printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":%s}}]}' $(ARTIFACT_TTL_DAYS) > /tmp/artifact-lifecycle.json
	@gcloud storage buckets update gs://$(BUCKET) --lifecycle-file=/tmp/artifact-lifecycle.json --project=$(PROJECT) >/dev/null
	@rm -f /tmp/artifact-lifecycle.json
	@echo "  lifecycle: delete after $(ARTIFACT_TTL_DAYS) day(s)"
	@num=$$(gcloud projects describe $(PROJECT) --format="value(projectNumber)"); \
	gcloud storage buckets add-iam-policy-binding gs://$(BUCKET) --project=$(PROJECT) \
		--member="serviceAccount:$$num-compute@developer.gserviceaccount.com" \
		--role="roles/storage.objectAdmin" >/dev/null; \
	echo "  runtime service account can read and write gs://$(BUCKET)"

gcp-url:
	$(call require_project)
	@echo "service:  $$(gcloud run services describe $(SERVICE) --project=$(PROJECT) --region=$(REGION) --format='value(status.url)')"
	@echo "api key:  gcloud secrets versions access latest --secret=HTTP_API_KEYS --project=$(PROJECT)"

gcp-logs:
	$(call require_project)
	gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="$(SERVICE)"' \
		--project=$(PROJECT) --limit 40 --freshness=30m --format="value(textPayload)"

# Let GitHub Actions deploy without holding a credential.
#
# The alternative is a service account key pasted into a repository secret,
# which never expires and can deploy to this project for as long as it exists.
# Workload Identity Federation trades it for an assertion: GitHub signs a token
# saying which repository and which ref is running, Google checks the signature
# against GitHub's public keys, and issues an access token that lasts minutes.
#
# The binding is what makes that safe. It names one repository, so a token
# minted by any other workflow anywhere on GitHub matches nothing here.
#
# The storage grants are for Cloud Build's source staging bucket, which
# `gcloud run deploy --source` uploads to. objectAdmin covers the objects, and
# a two-permission custom role covers finding the bucket: the deploy calls
# storage.buckets.list and then storage.buckets.get, and no predefined role
# carries those without also granting bucket create, delete, and IAM control,
# which a deploy never does. Established by deploying: objectAdmin alone fails
# on buckets.get, and adding get alone fails on buckets.list.
#
# Run once per project. Re-running is safe: each step tolerates the resource
# already existing.
GH_REPO ?= davecthomas/ai-api-unified-http
POOL ?= github
PROVIDER ?= $(SERVICE)
DEPLOYER ?= gh-deployer

gcp-cicd:
	$(call require_project)
	@num=$$(gcloud projects describe $(PROJECT) --format="value(projectNumber)"); \
	gcloud services enable iamcredentials.googleapis.com sts.googleapis.com --project=$(PROJECT); \
	gcloud iam workload-identity-pools create $(POOL) --project=$(PROJECT) \
		--location=global --display-name="GitHub Actions" 2>/dev/null || echo "  pool $(POOL) exists"; \
	gcloud iam workload-identity-pools providers create-oidc $(PROVIDER) --project=$(PROJECT) \
		--location=global --workload-identity-pool=$(POOL) \
		--issuer-uri="https://token.actions.githubusercontent.com" \
		--attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
		--attribute-condition="assertion.repository=='$(GH_REPO)'" \
		2>/dev/null || echo "  provider $(PROVIDER) exists"; \
	gcloud iam service-accounts create $(DEPLOYER) --project=$(PROJECT) \
		--display-name="GitHub Actions deployer" 2>/dev/null || echo "  service account $(DEPLOYER) exists"; \
	sa="$(DEPLOYER)@$(PROJECT).iam.gserviceaccount.com"; \
	printf '  waiting for the service account to exist'; \
	for attempt in 1 2 3 4 5 6 7 8 9 10; do \
		gcloud iam service-accounts describe "$$sa" --project=$(PROJECT) >/dev/null 2>&1 && break; \
		printf '.'; sleep 3; \
	done; echo; \
	gcloud iam service-accounts describe "$$sa" --project=$(PROJECT) >/dev/null 2>&1 \
		|| { echo "  service account $$sa never appeared; re-run this target"; exit 1; }; \
	gcloud iam roles create runSourceBucketReader --project=$(PROJECT) \
		--permissions=storage.buckets.get,storage.buckets.list \
		--title="Run source bucket reader" 2>/dev/null || echo "  custom role runSourceBucketReader exists"; \
	gcloud projects add-iam-policy-binding $(PROJECT) \
		--member="serviceAccount:$$sa" --role="projects/$(PROJECT)/roles/runSourceBucketReader" \
		--condition=None >/dev/null \
		&& echo "  granted runSourceBucketReader (storage.buckets.get + list)" \
		|| { echo "  FAILED to grant runSourceBucketReader"; exit 1; }; \
	for role in roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.writer roles/storage.objectAdmin; do \
		gcloud projects add-iam-policy-binding $(PROJECT) \
			--member="serviceAccount:$$sa" --role="$$role" --condition=None >/dev/null \
			&& echo "  granted $$role" \
			|| { echo "  FAILED to grant $$role"; exit 1; }; \
	done; \
	gcloud iam service-accounts add-iam-policy-binding "$$num-compute@developer.gserviceaccount.com" \
		--project=$(PROJECT) --member="serviceAccount:$$sa" \
		--role="roles/iam.serviceAccountUser" >/dev/null; \
	echo "  granted roles/iam.serviceAccountUser on the runtime service account"; \
	gcloud iam service-accounts add-iam-policy-binding "$$sa" --project=$(PROJECT) \
		--role="roles/iam.workloadIdentityUser" \
		--member="principalSet://iam.googleapis.com/projects/$$num/locations/global/workloadIdentityPools/$(POOL)/attribute.repository/$(GH_REPO)" >/dev/null; \
	echo "  bound $(GH_REPO) to $$sa"; \
	echo ""; \
	echo "Add these repository variables (Settings > Secrets and variables > Actions > Variables):"; \
	echo ""; \
	echo "  GCP_WIF_PROVIDER  projects/$$num/locations/global/workloadIdentityPools/$(POOL)/providers/$(PROVIDER)"; \
	echo "  GCP_DEPLOY_SA     $$sa"; \
	echo "  GCP_PROJECT       $(PROJECT)"; \
	echo ""; \
	echo "They identify a project, not a credential, so they are variables rather than secrets."
