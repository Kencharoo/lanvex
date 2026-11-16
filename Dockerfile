# syntax=docker/dockerfile:1
# Lanvex — single image serving the API and the built web UI.

# ---- frontend build -------------------------------------------------------------
FROM docker.io/library/node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- python dependencies ---------------------------------------------------------
FROM docker.io/library/python:3.13-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev

# ---- runtime ----------------------------------------------------------------------
FROM docker.io/library/python:3.13-slim
LABEL org.opencontainers.image.title="Lanvex" \
      org.opencontainers.image.description="Network Infrastructure, Simplified."
RUN groupadd --system lanvex && useradd --system --gid lanvex --home /app --shell /usr/sbin/nologin lanvex
# Mount point for Lanvex Core's static files (shared volume, written by Lanvex Core's user, uid 1000).
# Podman gives a new volume the owner of this folder when this container mounts it first; without
# the folder here, Lanvex Core could not write its static files under Podman.
RUN mkdir -p /srv/core-static && chown 1000:1000 /srv/core-static
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    LANVEX_ENV=production LANVEX_LOG_JSON=true
WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY --from=deps /app /app
COPY --from=web /web/dist /app/lanvex/static
COPY deploy/entrypoint.sh /usr/local/bin/lanvex-entrypoint
RUN chmod 0755 /usr/local/bin/lanvex-entrypoint && chown -R lanvex:lanvex /app
USER lanvex
EXPOSE 8000
# The health check is in docker-compose.yml: Podman builds OCI images, which cannot carry one (and
# warns about a HEALTHCHECK here on every build).
ENTRYPOINT ["lanvex-entrypoint"]
CMD ["serve"]
