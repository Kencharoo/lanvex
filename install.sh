#!/usr/bin/env bash
# Lanvex installer: asks a few questions, writes .env with generated passwords and keys, builds and
# starts the containers, waits until they are healthy, and prints how to sign in.
#
#   ./install.sh                       interactive
#   ./install.sh --yes                 Lanvex Core, HTTPS behind a reverse proxy, admin "admin"
#
# Options (skip the matching question):
#   --core                             keep network documentation in Lanvex Core, installed now
#   --netbox URL --netbox-token TOKEN  use an existing NetBox instead (--netbox-insecure: no TLS check)
#   --behind-proxy | --plain-http      HTTPS through a reverse proxy on this server, or plain HTTP
#   --port [ADDRESS:]PORT              where Lanvex listens (default 8080; with --behind-proxy only on
#                                      127.0.0.1). An address limits it, e.g. 100.64.0.5:8080
#   --admin NAME                       first administrator's username (default: admin)
#   --time-zone ZONE                   e.g. Europe/Berlin (default: this server's)
#   --yes                              accept defaults for everything not given
#   --no-start                         only write .env
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
fail() { printf '\nError: %s\n' "$*" >&2; exit 1; }
rand() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c "$1" || true; }  # tr ends on SIGPIPE

choice="" mode="" netbox_url="" netbox_token="" netbox_tls=true access="" port="" admin="" tz="" yes=false start=true
while [ $# -gt 0 ]; do
  case "$1" in
    --core) mode=core ;;
    --netbox) mode=netbox; netbox_url=${2:?--netbox needs a URL}; shift ;;
    --netbox-token) netbox_token=${2:?--netbox-token needs a value}; shift ;;
    --netbox-insecure) netbox_tls=false ;;
    --behind-proxy) access=proxy ;;
    --plain-http) access=plain ;;
    --port) port=${2:?--port needs a port, e.g. 8081 or 100.64.0.5:8081}; shift ;;
    --admin) admin=${2:?--admin needs a name}; shift ;;
    --time-zone) tz=${2:?--time-zone needs a zone}; shift ;;
    --yes) yes=true ;;
    --no-start) start=false ;;
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "Unknown option $1. See ./install.sh --help." ;;
  esac
  shift
done

ask() {  # ask VARIABLE "question" default
  local answer
  if $yes; then printf -v "$1" '%s' "$3"; return; fi
  read -r -p "$2 [$3]: " answer </dev/tty || answer=""
  printf -v "$1" '%s' "${answer:-$3}"
}

say "Lanvex installer"
say "================"

# ---- checks ----------------------------------------------------------------------------------------
[ -f .env ] && fail "Lanvex is already set up here (.env exists).
  Start it:          docker compose up -d
  Start over:        move .env somewhere safe first. It holds the keys to the existing data."
command -v docker >/dev/null || fail "Docker is not installed. The manual's installation chapter shows how."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is missing (the 'docker compose' command)."
if $start && ! docker info >/dev/null 2>&1; then
  fail "This user cannot use Docker. Run the installer with sudo, or add the user to the docker group."
fi

# ---- questions -------------------------------------------------------------------------------------
if [ -z "$mode" ]; then
  say ""
  say "Where should Lanvex keep your network documentation (networks, addresses, devices)?"
  say "  1) In Lanvex Core, installed now (recommended)"
  say "  2) In a NetBox you already run"
  ask choice "Choose 1 or 2" 1
  case "$choice" in 1) mode=core ;; 2) mode=netbox ;; *) fail "Please answer 1 or 2." ;; esac
fi
if [ "$mode" = netbox ]; then
  [ -n "$netbox_url" ] || ask netbox_url "NetBox address, e.g. https://netbox.example.com" ""
  [[ "$netbox_url" =~ ^https?://[^[:space:]]+$ ]] || fail "The NetBox address must start with http:// or https://."
  if [ -z "$netbox_token" ]; then
    $yes && fail "--netbox needs --netbox-token."
    read -r -s -p "NetBox API token (input hidden): " netbox_token </dev/tty; echo
  fi
  [ -n "$netbox_token" ] || fail "A NetBox API token is required. docs/operations.md lists the permissions it needs."
else
  [ -f core/lanvex/Dockerfile ] || fail "The Lanvex Core source is missing in ./core.
  Release archives include it; in a git checkout, run scripts/fetch-core.sh."
fi
if [ -z "$access" ]; then
  say ""
  say "How will people open Lanvex?"
  say "  1) Over HTTPS, through a reverse proxy on this server (recommended; see the manual's HTTPS chapter)"
  say "  2) Over plain HTTP on this network (for trying Lanvex out)"
  ask choice "Choose 1 or 2" 1
  case "$choice" in 1) access=proxy ;; 2) access=plain ;; *) fail "Please answer 1 or 2." ;; esac
fi
# Is anything already listening where Lanvex would? A program on all addresses, or on the same
# address, blocks it; one on a different single address does not.
port_taken() {  # port_taken ADDRESS PORT   (empty ADDRESS: all addresses)
  local listeners
  if command -v ss >/dev/null; then
    listeners=$(ss -Hltn 2>/dev/null | awk '{print $4}' | sed -n "s/:$2\$//p")
  else
    (exec 3<>"/dev/tcp/${1:-127.0.0.1}/$2") 2>/dev/null && return 0
    return 1
  fi
  [ -n "$listeners" ] || return 1
  [ -z "$1" ] && return 0
  printf '%s\n' "$listeners" | grep -qxE "0\.0\.0\.0|\*|\[::\]|\[::ffff:0\.0\.0\.0\]|${1//./\\.}"
}
while :; do
  if [ -z "$port" ]; then
    say ""
    ask port "Port for Lanvex on this server (add an address to limit it, e.g. 100.64.0.5:8080)" 8080
  fi
  if ! [[ "$port" =~ ^(([0-9]{1,3}\.){3}[0-9]{1,3}:)?[0-9]{1,5}$ ]] || [ "${port##*:}" -lt 1 ] || [ "${port##*:}" -gt 65535 ]; then
    fail "The port must be a number from 1 to 65535, optionally after an IPv4 address: 8080 or 100.64.0.5:8080."
  fi
  if [[ "$port" != *:* ]] && [ "$access" = proxy ]; then port="127.0.0.1:$port"; fi
  address=""
  if [[ "$port" == *:* ]]; then address=${port%:*}; fi
  port_taken "$address" "${port##*:}" || break
  $yes && fail "Port ${port##*:} is already in use on this server. Choose another with --port, e.g. --port 8081."
  say "Port ${port##*:} is already in use on this server by another program. Please choose another."
  port=""
done
[ -n "$admin" ] || ask admin "Username for the first administrator" admin
[[ "$admin" =~ ^[A-Za-z0-9_.@-]{2,64}$ ]] || fail "Usernames use letters, digits and . _ @ - (2 to 64 characters)."
if [ -z "$tz" ]; then
  tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)
  tz=${tz:-UTC}
fi

# ---- .env ------------------------------------------------------------------------------------------
admin_password=$(rand 20)
umask 077
{
  say "# Written by install.sh on $(date -u +%Y-%m-%d). Keep a copy somewhere safe: it holds the keys"
  say "# to Lanvex's data. Never commit it. docs/configuration.md explains every setting."
  say ""
  say "LANVEX_POSTGRES_PASSWORD=$(rand 40)"
  say "LANVEX_SECRET_KEY=$(rand 64)"
  say "LANVEX_BOOTSTRAP_ADMIN_USERNAME=$admin"
  say "LANVEX_BOOTSTRAP_ADMIN_PASSWORD=$admin_password"
  say ""
  if [ "$access" = proxy ]; then
    say "# A reverse proxy on this server provides HTTPS (manual, HTTPS chapter)."
    say "LANVEX_HTTP_PORT=$port"
    say 'LANVEX_TRUSTED_PROXIES=["172.31.250.1/32"]'
  else
    say "# Plain HTTP. Fine for trying Lanvex out; use HTTPS for real use."
    say "LANVEX_HTTP_PORT=$port"
    say "LANVEX_COOKIE_SECURE=false"
  fi
  say ""
  if [ "$mode" = core ]; then
    say "# Lanvex Core: the NetBox-based application bundled with Lanvex, served at /core/."
    say "COMPOSE_PROFILES=core"
    service_token="nbt_$(rand 12).$(rand 40)"
    say "LANVEX_NETBOX_URL=http://core:8080/core"
    say "LANVEX_NETBOX_TOKEN=$service_token"
    say "LANVEX_NETBOX_PUBLIC_URL=/core"
    say "LANVEX_CORE_UPSTREAM_URL=http://core:8080"
    say "LANVEX_CORE_STATIC_DIR=/srv/core-static"
    say "LANVEX_CORE_PROXY_SECRET=$(rand 64)"
    say "LANVEX_CORE_SERVICE_TOKEN=$service_token"
    say "LANVEX_CORE_DB_PASSWORD=$(rand 40)"
    say "LANVEX_CORE_SECRET_KEY=$(rand 64)"
    say "LANVEX_CORE_API_TOKEN_PEPPER=$(rand 64)"
    say "LANVEX_CORE_TIME_ZONE=$tz"
  else
    say "LANVEX_NETBOX_URL=$netbox_url"
    say "LANVEX_NETBOX_TOKEN=$netbox_token"
    say "LANVEX_NETBOX_VERIFY_TLS=$netbox_tls"
  fi
  say ""
  say "# Changing server configuration through Lanvex: off until you need it (docs/changes.md)."
  say "LANVEX_AUTOMATION_ENABLED=false"
} > .env
umask 022
say ""
say "Settings written to .env."
$start || { say "Start Lanvex with: docker compose up -d --build"; exit 0; }

# ---- start -----------------------------------------------------------------------------------------
say ""
say "Building and starting the containers. The first build downloads and compiles a lot and can take"
say "10 to 20 minutes."
start_failed() {
  printf '\nError: %s\n\n' "$1" >&2
  say "Your settings are in .env, including the first administrator's password"
  say "(LANVEX_BOOTSTRAP_ADMIN_PASSWORD; username: $admin). Once the cause is fixed, start Lanvex with:"
  say "  docker compose up -d --build"
  say "and remove the password from .env after signing in for the first time."
  exit 1
}
docker compose up -d --build || start_failed "Starting the containers failed; the output above says why."

health() { docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
  "$(docker compose ps -q "$1")" 2>/dev/null || echo missing; }
wait_healthy() {  # wait_healthy SERVICE "description" MINUTES
  local deadline=$(( $(date +%s) + $3 * 60 )) state
  printf 'Waiting for %s' "$2"
  while :; do
    state=$(health "$1")
    case "$state" in
      healthy) printf ' ready.\n'; return 0 ;;
      unhealthy|exited|dead|missing)
        printf ' %s.\n\nThe last lines of its log:\n' "$state"; docker compose logs --tail 40 "$1"; return 1 ;;
    esac
    [ "$(date +%s)" -lt "$deadline" ] || { printf ' still not ready after %s minutes.\n' "$3"; return 1; }
    printf '.'; sleep 10
  done
}
wait_healthy lanvex "Lanvex" 5 || start_failed "Lanvex did not start. See the log above and the manual's troubleshooting chapter."
if [ "$mode" = core ]; then
  say "Lanvex Core sets up its database on the first start, which takes several minutes."
  wait_healthy core "Lanvex Core" 20 || start_failed "Lanvex Core did not start. See the log above; 'docker compose logs core' shows all of it."
fi

# The administrator exists now; the password does not need to stay in .env.
sed -i 's/^LANVEX_BOOTSTRAP_ADMIN_PASSWORD=.*/LANVEX_BOOTSTRAP_ADMIN_PASSWORD=/' .env

say ""
say "Lanvex is running."
say ""
if [ "$access" = proxy ] && [ "${port%:*}" = 127.0.0.1 ]; then
  say "  Address:   http://127.0.0.1:${port##*:} on this server. Set up HTTPS next (manual, HTTPS chapter);"
  say "             until then, an SSH tunnel works: ssh -L ${port##*:}:127.0.0.1:${port##*:} <you>@<this server>"
elif [[ "$port" == *:* ]]; then
  say "  Address:   http://$port"
else
  say "  Address:   http://<this server's address>:$port"
fi
say "  Username:  $admin"
say "  Password:  $admin_password"
say ""
say "Sign in and change the password (Settings). It is shown only now and was removed from .env."
[ "$mode" = core ] && say "Lanvex Core is in the menu on the left, and at /core/."
say "Back up .env: without it, Lanvex cannot read the credentials it stores."
