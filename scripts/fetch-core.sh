#!/usr/bin/env bash
# Puts the Lanvex Core source pinned in core.lock into ./core, where docker-compose.yml builds it.
# Release archives already contain ./core; this is for working from the git repositories.
#
#   scripts/fetch-core.sh [lanvex-core repository: a path or a git URL; default ../lanvex-core]
set -euo pipefail
cd "$(dirname "$0")/.."
commit=$(sed -n 's/^commit=//p' core.lock)
source=${1:-../lanvex-core}
[ -n "$commit" ] || { echo "core.lock names no commit." >&2; exit 1; }

repo=$source
cleanup=""
if [ ! -d "$source/.git" ] && [ ! -f "$source/HEAD" ]; then
  repo=$(mktemp -d)
  cleanup=$repo
  git clone --quiet --filter=blob:none --no-checkout "$source" "$repo"
fi
git -C "$repo" cat-file -e "$commit^{commit}" 2>/dev/null || {
  echo "Commit $commit (from core.lock) is not in $source." >&2; exit 1; }

rm -rf core.new && mkdir core.new
git -C "$repo" archive "$commit" | tar -x -C core.new
echo "$commit" > core.new/.lanvex-core-commit
rm -rf core && mv core.new core
[ -n "$cleanup" ] && rm -rf "$cleanup"
echo "Lanvex Core $(sed -n 's/^tag=//p' core.lock) ($commit) is in ./core."
