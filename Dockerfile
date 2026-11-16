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
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=4).status == 200 else 1)"
ENTRYPOINT ["lanvex-entrypoint"]
CMD ["serve"]
