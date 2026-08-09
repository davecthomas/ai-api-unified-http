# Container image for ai-api-unified-http.
#
# Two stages so the runtime image carries no build toolchain and no Poetry.
# Dependencies are resolved from the lockfile into a virtualenv, which the
# runtime stage copies whole. Using a venv rather than a prefix install means
# the interpreter finds its own packages with no PYTHONPATH to keep in sync.

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=2.1.3

RUN pip install "poetry==${POETRY_VERSION}" poetry-plugin-export

WORKDIR /build
COPY pyproject.toml poetry.lock ./

# Export from the lockfile rather than resolving again, so the image installs
# the same versions the test suite ran against.
RUN python -m venv /venv \
    && poetry export --format requirements.txt --output requirements.txt --without-hashes \
    && /venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.13-slim AS runtime

# Non-root. Cloud Run does not require it, but a container that never needs
# root should not run as root.
RUN useradd --create-home --uid 10001 service

COPY --from=builder /venv /venv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:${PATH}" \
    PYTHONPATH="/app/src"

WORKDIR /app
COPY --chown=service:service src/ ./src/
COPY --chown=service:service config/ ./config/

USER service

# Cloud Run supplies PORT and expects the container to listen on it.
ENV PORT=8080
EXPOSE 8080

# gunicorn with uvicorn workers, as docs/technical-design.md specifies for
# production. create_app is a factory, so it is called with the trailing
# parentheses gunicorn supports.
#
# The timeout is generous because a streaming request occupies its worker for
# the life of the stream; Cloud Run's own request timeout is the outer bound.
CMD exec gunicorn "ai_api_unified_http.app:create_app()" \
    --bind "0.0.0.0:${PORT}" \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "${WEB_CONCURRENCY:-2}" \
    --timeout "${WEB_TIMEOUT:-3600}" \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile -
