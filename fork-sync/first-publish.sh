#!/usr/bin/env bash
# Build a new fork's first-publish tarball exactly the way CI would, then print
# the one command the operator runs. npm trusted publishing can only attach to
# a package that already exists, so each fork's first version is published by
# hand (see fork-sync/README.md "Onboarding a new fork").
#
# Usage: first-publish.sh <fork-checkout> [out-dir]   (out-dir defaults to ~/Desktop)
#
# Settings come from the `with:` of the fork-sync step in the fork's own
# .github/workflows/upstream-sync.yml, the gate is that step's test_cmd and
# build_cmd, and stamping is the action's own upstream-sync.sh (`--stamp`, beside
# this script), so nothing here restates CI's logic.
set -euo pipefail
[ $# -ge 1 ] || { sed -n '7p' "$0"; exit 2; }
repo="$(cd "$1" && pwd)"
out="${2:-$HOME/Desktop}"
wf="$repo/.github/workflows/upstream-sync.yml"
script="$(cd "$(dirname "$0")" && pwd)/upstream-sync.sh"
[ -f "$wf" ] || { echo "first-publish: $repo has no fork-sync caller ($wf)" >&2; exit 1; }
[ -f "$script" ] || { echo "first-publish: $script is missing" >&2; exit 1; }

setting() { # key -> value from the fork-sync step's `with:` block ("" when absent)
  # Scoped to the step that uses schuettc/tools-actions/fork-sync@...; values are
  # one-line YAML scalars, double-quoted or plain.
  awk -v key="$1" '
    /uses:[[:space:]]*schuettc\/tools-actions\/fork-sync@/ { in_step = 1; next }
    in_step && /^[[:space:]]*-[[:space:]]/ { in_step = 0 }
    in_step && $0 ~ "^[[:space:]]+" key ":" {
      v = $0; sub("^[[:space:]]+" key ":[[:space:]]*", "", v); sub(/[[:space:]]+$/, "", v)
      if (v ~ /^".*"$/) v = substr(v, 2, length(v) - 2)
      print v; exit
    }' "$wf"
}
PKG_NAME="$(setting pkg_name)"; PKG_DIR="$(setting pkg_dir)"
TEST_CMD="$(setting test_cmd)"; BUILD_CMD="$(setting build_cmd)"
[ -n "$PKG_NAME" ] && [ -n "$PKG_DIR" ] || { echo "first-publish: pkg_name/pkg_dir missing from the fork-sync step in $wf" >&2; exit 1; }

cd "$repo"
git fetch -q origin schuettc-publish
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/schuettc-publish)" ] || {
  echo "first-publish: check out origin/schuettc-publish first (HEAD differs), so the tarball is what CI would publish" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "first-publish: working tree has changes" >&2; exit 1; }

FORK="${FORK:-$(git remote get-url origin | sed -E 's#^(git@github.com:|https://github.com/)##; s#\.git$##')}"
DIR_FIELD="${PKG_DIR#./}"; DIR_FIELD="${DIR_FIELD%/}"; [ "$DIR_FIELD" = "." ] && DIR_FIELD=""
base="$(node -pe "require('./${DIR_FIELD:+$DIR_FIELD/}package.json').version")"
VER="${base}-schuettc.1"
if npm view "$PKG_NAME" name >/dev/null 2>&1; then
  echo "first-publish: $PKG_NAME already exists on npm; publish through the workflow (force dispatch) instead" >&2; exit 1
fi

echo "== $PKG_NAME@$VER from $FORK ($(git rev-parse --short HEAD))"
[ -z "$TEST_CMD" ] || { echo "-- test: $TEST_CMD"; bash -c "$TEST_CMD"; }
[ -z "$BUILD_CMD" ] || { echo "-- build: $BUILD_CMD"; bash -c "$BUILD_CMD"; }

pub="$(mktemp -d)"; trap 'rm -rf "$pub"' EXIT
rsync -a --exclude .git --exclude node_modules "${PKG_DIR%/}/" "$pub/"
PKG_NAME="$PKG_NAME" VER="$VER" FORK="$FORK" DIR_FIELD="$DIR_FIELD" bash "$script" --stamp "$pub/package.json"
# The one difference from CI: provenance needs CI's OIDC token, so a local
# publish must not request it.
node -e 'const fs=require("fs"),f=process.argv[1],p=JSON.parse(fs.readFileSync(f));
  if (p.publishConfig) { delete p.publishConfig.provenance; if (!Object.keys(p.publishConfig).length) delete p.publishConfig; }
  fs.writeFileSync(f, JSON.stringify(p, null, 2) + "\n");' "$pub/package.json"
mkdir -p "$out"
tgz="$(cd "$pub" && npm pack --ignore-scripts --pack-destination "$out" 2>/dev/null | tail -1)"

cat <<EOF

Tarball: $out/$tgz

Publish (as yourself; run \`npm login\` first if \`npm whoami\` fails):

  npm publish "$out/$tgz" --access public --ignore-scripts --tag latest

Then add the trusted publisher at https://www.npmjs.com/package/$PKG_NAME/access
(Settings -> Trusted Publisher -> GitHub Actions):
  user ${FORK%%/*} · repository ${FORK#*/} · workflow upstream-sync.yml · environment (empty) · permission "npm publish"
EOF
