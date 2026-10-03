# Aster persistent identity runtime.
#
# The image carries the identity runtime, not the identity. State, memory,
# relationships, provenance, and email jobs live in a mounted volume; secrets
# are injected at run time and never baked into a layer.
#
# Build:
#   docker build -f deploy/Dockerfile -t identityos-aster .
#
# Run (local volume, secrets from files, restart on crash):
#   docker run -d --name aster \
#     --restart unless-stopped \
#     -p 127.0.0.1:8787:8787 \
#     -v "$HOME/.identityos/data:/data" \
#     -v "$HOME/.identityos/secrets:/run/secrets:ro" \
#     --env-file "$HOME/.identityos/secrets/identityos.env" \
#     --env-file "$HOME/.identityos/secrets/email.env" \
#     --env-file "$HOME/.identityos/secrets/providers.env" \
#     identityos-aster
#
# Health check hits the readiness endpoint, so a container that cannot reach
# its store reports unhealthy rather than looking alive.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

# Dependencies first so a source edit does not invalidate the wheel layer.
FROM base AS deps
WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

FROM deps AS runtime

# git is needed for capability acquisition (Prometheus clones and installs);
# ca-certificates for outbound TLS to model and mail providers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# The runtime writes only to /data and /tmp.
RUN useradd --create-home --uid 10001 aster \
    && mkdir -p /data \
    && chown -R aster:aster /app /data
USER aster

ENV IDENTITY_STORE=/data \
    IDENTITY_BACKEND=sqlite \
    ASTER_DATA_DIR=/data \
    ASTER_LOG_FORMAT=json

EXPOSE 8787

# Readiness, not just liveness: confirms the process answers AND its store is
# readable, so a container with a broken volume never reports ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8787/ready || exit 1

# SIGTERM reaches PID 1 (the entrypoint) so it can forward to both children;
# the operator finishes in-flight work and persists state before exiting.
STOPSIGNAL SIGTERM

# Two processes are required: the operator (identity runtime, sole writer for
# email jobs and sends) and the presence endpoint (readiness, health, dashboard,
# and the Gmail push receiver). Running only the operator would expose 8787 and
# health-check /ready, which nothing could answer.
ENTRYPOINT ["python", "-m", "deploy.container_entrypoint"]
CMD ["--iterations", "-1", "--interval", "300", "--email-interval", "20"]
