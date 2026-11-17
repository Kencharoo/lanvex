#!/usr/bin/env bash
# shellcheck disable=SC2015  # "A && B || no ..." is meant: fail unless both hold
# Tests lanvexctl's own logic without containers: releases are small archives whose install.sh only
# records how it was called, and "docker" is a stand-in that answers like a running installation.
# The full path (real containers, backup and restore into a fresh installation) runs in CI.
set -euo pipefail
exec < /dev/null  # no terminal: lanvexctl must never wait for an answer here
root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export HOME="$work/home" LANVEX_HOME="$work/home/lanvex" LANVEX_BIN="$work/home/.local/bin" LANVEX_REPO=example/lanvex
mkdir -p "$HOME" "$work/bin" "$work/dist"
log="$work/calls"
pass=0
ok() { pass=$((pass + 1)); printf 'ok    %s\n' "$1"; }
no() { printf 'FAIL  %s\n' "$1" >&2; exit 1; }

# The container runtime: Docker Compose v2, every service healthy, databases that dump "DUMP-<db>".
cat > "$work/bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$1" in
  --version) echo "Docker version 27.0.0" ;;
  info) ;;
  compose) if [ "${2:-}" = version ]; then echo "Docker Compose version v2.29.0"; fi ;;
  ps) all="$*"; svc=${all##*service=}; echo "${svc%% *}-id" ;;
  inspect) echo healthy ;;
  logs) echo "log line" ;;
  exec)
    shift; [ "$1" = -i ] && shift; shift
    case "$1" in
      pg_dump) echo "DUMP-$5" ;;
      tar) [ "$2" = -C ] && printf 'MEDIA' ;;
      *) cat >/dev/null ;;
    esac ;;
esac
EOF
chmod +x "$work/bin/docker"
export PATH="$work/bin:$PATH" CALLS="$log"

release() {  # release VERSION -> dist/lanvex-VERSION.zip and its .sha256
  local d="$work/src/lanvex-$1"
  mkdir -p "$d/backend/lanvex"
  printf '__version__ = "%s"\n' "$1" > "$d/backend/lanvex/__init__.py"
  cp "$root/lanvexctl" "$d/lanvexctl"
  cat > "$d/install.sh" <<'EOF'
#!/usr/bin/env bash
echo "install.sh $* in $(pwd)" >> "$CALLS"
if [ "${1:-}" = --update ]; then cp "$2/.env" .env; else printf 'COMPOSE_PROFILES=core\nLANVEX_HTTP_PORT=8080\nLANVEX_POSTGRES_PASSWORD=pw-%s\n' "$RANDOM" > .env; fi
EOF
  chmod +x "$d/install.sh"
  (cd "$work/src" && python3 -m zipfile -c "$work/dist/lanvex-$1.zip" "lanvex-$1" >/dev/null)
  (cd "$work/dist" && sha256sum "lanvex-$1.zip" > "lanvex-$1.zip.sha256")
}
release 1.1.0
release 1.2.0
release 1.3.0
ctl="$root/lanvexctl"

# install
"$ctl" install --file "$work/dist/lanvex-1.1.0.zip" -- --core --yes > "$work/out" 2>&1 || { cat "$work/out"; no "install"; }
[ "$(readlink "$LANVEX_HOME/current")" = releases/1.1.0 ] || no "install links current to the release"
grep -q "install.sh --core --yes in $LANVEX_HOME/releases/1.1.0" "$log" || no "install passes options to install.sh"
[ -x "$HOME/.local/bin/lanvexctl" ] || no "install puts lanvexctl into ~/.local/bin"
ok "install from an archive: release directory, current link, lanvexctl in ~/.local/bin"
if "$ctl" install --file "$work/dist/lanvex-1.2.0.zip" > "$work/out" 2>&1; then no "a second install is refused"; fi
grep -q "already installed" "$work/out" || no "says why a second install is refused"
ok "a second install is refused"

# a damaged archive
cp "$work/dist/lanvex-1.2.0.zip" "$work/bad.zip"; cp "$work/dist/lanvex-1.2.0.zip.sha256" "$work/bad.zip.sha256"
sed -i 's/lanvex-1.2.0.zip/bad.zip/' "$work/bad.zip.sha256"; printf 'x' >> "$work/bad.zip"
if "$ctl" update --file "$work/bad.zip" > "$work/out" 2>&1; then no "a damaged archive is refused"; fi
grep -q "does not match" "$work/out" || no "says the checksum does not match"
ok "an archive that does not match its checksum is refused"

# status and logs
"$ctl" status > "$work/out" || { cat "$work/out"; no "status"; }
grep -q "Lanvex 1.1.0" "$work/out" && grep -q "core-worker *healthy" "$work/out" || no "status lists version and services"
ok "status: version, directory, services"
"$ctl" logs core | grep -q "log line" || no "logs"
ok "logs"

# backup
"$ctl" backup "$work/b.tar.gz" > "$work/out" || { cat "$work/out"; no "backup"; }
mkdir "$work/b" && tar -C "$work/b" -xzf "$work/b.tar.gz"
[ "$(cat "$work/b/lanvex.dump")" = DUMP-lanvex ] && [ "$(cat "$work/b/core.dump")" = DUMP-lanvex_core ] || no "backup holds both databases"
[ "$(cat "$work/b/core-media.tar")" = MEDIA ] && cmp -s "$work/b/env" "$LANVEX_HOME/current/.env" || no "backup holds the uploads and .env"
grep -q "^version=1.1.0" "$work/b/manifest" || no "backup names its version"
[ "$(stat -c %a "$work/b.tar.gz")" = 600 ] || no "backup is readable only by its owner"
ok "backup: databases, uploads, .env and version, mode 600"

# update: a backup first, then install.sh --update from the running release
"$ctl" update --file "$work/dist/lanvex-1.2.0.zip" > "$work/out" 2>&1 || { cat "$work/out"; no "update"; }
grep -q "install.sh --update $LANVEX_HOME/releases/1.1.0 in $LANVEX_HOME/releases/1.2.0" "$log" || no "update runs install.sh --update"
[ "$(readlink "$LANVEX_HOME/current")" = releases/1.2.0 ] || no "update moves current"
ls "$LANVEX_HOME"/backups/lanvex-1.1.0-before-update-*.tar.gz >/dev/null || no "update backs up first"
"$ctl" update --file "$work/dist/lanvex-1.3.0.zip" > "$work/out" 2>&1 || { cat "$work/out"; no "second update"; }
[ ! -d "$LANVEX_HOME/releases/1.1.0" ] && [ -d "$LANVEX_HOME/releases/1.2.0" ] || no "keeps the previous release only"
ok "update: backup first, install.sh --update, current moved, one previous release kept"

# restore: newer backups are refused; confirmation; the restore itself
"$ctl" backup "$work/b13.tar.gz" >/dev/null
sed -i 's/^version=.*/version=9.0.0/' "$work/b/manifest" && tar -C "$work/b" -czf "$work/newer.tar.gz" .
if "$ctl" restore "$work/newer.tar.gz" --yes > "$work/out" 2>&1; then no "a newer backup is refused"; fi
grep -q "newer than 1.3.0" "$work/out" || no "says the backup is newer"
if "$ctl" restore "$work/b13.tar.gz" < /dev/null > "$work/out" 2>&1; then no "restore without a terminal needs --yes"; fi
: > "$log"
"$ctl" restore "$work/b13.tar.gz" --yes > "$work/out" 2>&1 || { cat "$work/out"; tail -5 "$log"; no "restore"; }
grep -q "exec -i lanvex-db-id dropdb -U lanvex --force --if-exists lanvex" "$log" || { grep exec "$log"; no "restore replaces Lanvex's database"; }
grep -q "exec -i core-db-id pg_restore -U lanvex_core -d lanvex_core" "$log" || no "restore replaces Lanvex Core's database"
grep -q "compose up -d --force-recreate" "$log" || no "restore recreates the containers"
[ -f "$LANVEX_HOME/current/.env.before-restore" ] || no "restore keeps the previous .env"
ok "restore: version check, confirmation, databases replaced, containers recreated"

# uninstall
if "$ctl" uninstall --purge < /dev/null > "$work/out" 2>&1; then no "uninstall needs confirmation"; fi
"$ctl" uninstall --purge --yes > "$work/out" 2>&1 || { cat "$work/out"; no "uninstall"; }
grep -q "compose down --volumes" "$log" || no "purge removes the volumes"
[ ! -e "$LANVEX_HOME/current" ] && [ -d "$LANVEX_HOME/backups" ] || no "purge keeps the backups only"
ok "uninstall --purge: containers and volumes gone, backups kept"

# an installation unpacked by hand into ~/lanvex, before lanvexctl
mkdir -p "$work/legacy" && export LANVEX_HOME="$work/legacy/lanvex"
mkdir -p "$LANVEX_HOME/backend/lanvex" && printf '__version__ = "1.0.1"\n' > "$LANVEX_HOME/backend/lanvex/__init__.py"
printf 'LANVEX_POSTGRES_PASSWORD=old\n' > "$LANVEX_HOME/.env"
"$ctl" update --file "$work/dist/lanvex-1.2.0.zip" > "$work/out" 2>&1 || { cat "$work/out"; no "update of a hand-made installation"; }
grep -q "install.sh --update $LANVEX_HOME in $LANVEX_HOME/releases/1.2.0" "$log" || no "updates from the hand-made directory"
[ "$(readlink "$LANVEX_HOME/current")" = releases/1.2.0 ] && cmp -s "$LANVEX_HOME/.env" "$LANVEX_HOME/current/.env" || no "takes its .env along"
ok "update of an installation unpacked by hand: taken over into releases/"

printf '%s passed\n' "$pass"
