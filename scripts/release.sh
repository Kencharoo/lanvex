#!/usr/bin/env bash
# Builds the release archive: Lanvex at HEAD plus the Lanvex Core source pinned in core.lock, in one
# zip that install.sh can install from without git or network access to either repository.
#
#   scripts/release.sh [lanvex-core repository; default ../lanvex-core] [output directory; default .]
set -euo pipefail
cd "$(dirname "$0")/.."
core_repo=${1:-../lanvex-core}
out=${2:-.}
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' backend/lanvex/__init__.py)
commit=$(sed -n 's/^commit=//p' core.lock)
if [ -n "$(git status --porcelain)" ]; then
  echo "Commit your changes first; the archive is built from HEAD." >&2; exit 1
fi
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
dir="$work/lanvex-$version"
mkdir -p "$dir/core"
git archive HEAD | tar -x -C "$dir"
git -C "$core_repo" archive "$commit" | tar -x -C "$dir/core"
echo "$commit" > "$dir/core/.lanvex-core-commit"
(cd "$work" && python3 -m zipfile -c "lanvex-$version.zip" "lanvex-$version" >/dev/null)
mv "$work/lanvex-$version.zip" "$out/"
echo "$out/lanvex-$version.zip (Lanvex $version with Lanvex Core $(sed -n 's/^tag=//p' core.lock))"
