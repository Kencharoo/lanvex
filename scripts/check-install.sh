#!/usr/bin/env bash
# Checks a running Lanvex installation from this directory: every container healthy and never
# restarted, no errors in the logs, sign-in works, NetBox or Lanvex Core answers, and Lanvex Core is
# reachable only with a Lanvex session. Changes nothing.
#
#   scripts/check-install.sh [URL] [USERNAME] [PASSWORD]
#
# URL defaults to http://127.0.0.1:<port from .env> (pass "" to keep it). With a username but no
# password, it asks for the password; without a username, the sign-in checks are skipped.
set -euo pipefail
cd "$(dirname "$0")/.."
port=$(sed -n 's/^LANVEX_HTTP_PORT=//p' .env 2>/dev/null || true)
url=${1:-http://127.0.0.1:${port##*:}}
user=${2:-}
password=${3:-}
if [ -n "$user" ] && [ -z "$password" ] && [ -t 0 ]; then
  read -r -s -p "Password for $user: " password; echo
fi
failures=0
ok() { printf 'ok    %s\n' "$*"; }
bad() { printf 'FAIL  %s\n' "$*"; failures=$((failures + 1)); }

# ---- containers ------------------------------------------------------------------------------------
deadline=$(( $(date +%s) + 600 ))
while :; do
  states=$(docker compose ps -a --format '{{.Service}} {{if .Health}}{{.Health}}{{else}}{{.State}}{{end}}')
  if ! grep -q ' starting$' <<<"$states" || [ "$(date +%s)" -ge "$deadline" ]; then break; fi
  sleep 10
done
if [ -z "$states" ]; then
  bad "no Lanvex containers here (run this in the directory you installed Lanvex from)"
else
  while read -r service state; do
    if [ "$state" = healthy ]; then ok "$service is healthy"; else bad "$service is $state"; fi
  done <<<"$states"
  while read -r name restarts; do
    if [ "$restarts" != 0 ]; then bad "${name#/} restarted $restarts time(s)"; fi
  done < <(docker compose ps -aq | xargs docker inspect --format '{{.Name}} {{.RestartCount}}')
  mapfile -t services < <(docker compose ps --services | grep -xE 'lanvex|core|core-worker' || true)
  if errors=$(docker compose logs --no-color "${services[@]}" 2>&1 | grep -E 'Traceback|\[ERROR\]|"level": ?"(error|critical)"'); then
    bad "errors in the logs:"; printf '%s\n' "$errors" | tail -20 | sed 's/^/        /'
  else
    ok "no errors in the logs of: ${services[*]}"
  fi
fi

# ---- Lanvex ----------------------------------------------------------------------------------------
if curl -fsS -o /dev/null "$url/api/v1/health/ready"; then ok "Lanvex is ready at $url"; else bad "Lanvex does not answer at $url"; fi
if [ -n "$password" ]; then
  jar=$(mktemp); trap 'rm -f "$jar"' EXIT
  body=$(printf '{"username": "%s", "password": "%s"}' "$user" "$password")
  if curl -fsS -o /dev/null -c "$jar" -H 'Content-Type: application/json' -d "$body" "$url/api/v1/auth/login"; then
    ok "signed in as $user"
    netbox=$(curl -fsS -b "$jar" "$url/api/v1/system/netbox")
    if grep -q '"reachable":true' <<<"$netbox" && grep -q '"authenticated":true' <<<"$netbox"; then
      ok "network documentation reachable and signed in: $(grep -o '"version":"[^"]*"' <<<"$netbox")"
    else
      bad "network documentation not usable: $netbox"
    fi
  else
    bad "sign-in as $user failed"
  fi
fi

# ---- Lanvex Core -----------------------------------------------------------------------------------
if grep -q '^COMPOSE_PROFILES=.*core' .env 2>/dev/null; then
  code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Accept: text/html' "$url/core/")
  if [ "$code" = 303 ]; then ok "Lanvex Core sends signed-out visitors to Lanvex's sign-in"; else bad "Lanvex Core without a session: HTTP $code"; fi
  code=$(curl -s -o /dev/null -w '%{http_code}' "$url/core/static/lanvex_core/lanvex-core.css")
  if [ "$code" = 200 ]; then ok "Lanvex Core's styles are served"; else bad "Lanvex Core's styles: HTTP $code"; fi
  if [ -n "$password" ]; then
    page=$(curl -s -b "$jar" -H 'Accept: text/html' "$url/core/ipam/prefixes/")
    if grep -q '<title>Prefixes | Lanvex Core' <<<"$page"; then
      ok "Lanvex Core opens as $user"
    else
      bad "Lanvex Core did not open as $user"
    fi
  fi
fi

echo
[ "$failures" -eq 0 ] && { echo "The installation looks good."; exit 0; }
echo "$failures problem(s) found."; exit 1
