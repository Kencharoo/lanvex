#!/bin/sh
set -eu

case "${1:-serve}" in
  serve)
    if [ "${LANVEX_MIGRATE_ON_START:-true}" = "true" ]; then
      lanvex migrate
    fi
    exec uvicorn lanvex.main:app \
      --host 0.0.0.0 --port "${LANVEX_PORT:-8000}" \
      --workers "${LANVEX_WORKERS:-2}" \
      --no-proxy-headers --no-server-header
    ;;
  *)
    # Operator commands, e.g. `docker compose run --rm lanvex create-user alice --role admin`
    exec lanvex "$@"
    ;;
esac
