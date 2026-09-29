#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2034
# (check() evals its single-quoted assertion later, so SC2016 is intended, and
# variables such as OUT/RC/rc are read only inside those strings: SC2034.)
# Hermetic tests for upstream-sync.sh: synthetic upstream + fork repos, a local
# bare "origin", and npm/gh shims that record calls. No network, no real publish.
# Usage: test.sh   (exit 0 = all scenarios pass)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# The runner uses bash 5, whose ERR-trap/subshell semantics differ from macOS's
# bash 3.2 (a 3.2-only pass once hid a double-issue bug). Under an old bash, re-run
# inside a Debian container so the script is exercised the way CI runs it.
if [ "${BASH_VERSINFO[0]}" -lt 4 ]; then
  if ! { command -v docker >/dev/null && docker info >/dev/null 2>&1; }; then
    echo "test.sh: needs bash >= 4 (CI runs bash 5) or docker" >&2; exit 2
  fi
  extra=(); if [ -n "${SCRIPT:-}" ]; then extra=(-v "$SCRIPT:/sut.sh:ro" -e SCRIPT=/sut.sh); fi
  exec docker run --rm -v "$HERE/..:/fs:ro" ${extra[@]+"${extra[@]}"} node:22-bookworm bash -c \
    'apt-get -qq update >/dev/null && apt-get -qq install -y rsync >/dev/null 2>&1 && bash /fs/tests/test.sh'
fi
# Hermetic git: a developer's global config (commit/tag signing, hooks) must not
# reach the fixture repos; with signing on, every fixture commit waits on the
# signer and the suite hangs.
GIT_CONFIG_GLOBAL="$(mktemp)"; printf '[user]\n\tname = t\n\temail = t@t\n' > "$GIT_CONFIG_GLOBAL"
export GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM=1
SCRIPT_KIT="$HERE/../upstream-sync.sh"
SCRIPT="${SCRIPT:-$HERE/../upstream-sync.sh}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
pass=0; failn=0
ok()   { echo "  PASS $1"; pass=$((pass+1)); }
fail() { echo "  FAIL $1"; failn=$((failn+1)); }
check() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }

# --- shims -------------------------------------------------------------------
mkdir -p "$W/bin"
cat > "$W/bin/npm" <<'EOF'
#!/usr/bin/env bash
echo "npm $(printf '%q ' "$@")" >> "$SHIM_LOG"
case "$1" in
  view) [ "${NPM_VIEW_404:-}" = 1 ] && { echo "npm ERR! 404" >&2; exit 1; }; echo "${NPM_PUBLISHED:-[]}";;
  pack) cp package.json "$SHIM_DIR/packed-package.json"; echo "+ packed";;
  publish)
    [ "${NPM_PUBLISH_FAIL:-}" = 1 ] && { echo "npm ERR! 403" >&2; exit 1; }
    cp package.json "$SHIM_DIR/published-package.json"
    [ -f dist/index.js ] && cp dist/index.js "$SHIM_DIR/published-dist.js"
    echo "+ published";;
  *) exit 0;;
esac
EOF
cat > "$W/bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $(printf '%q ' "$@")" >> "$SHIM_LOG"
case "$1 $2" in
  "issue list") echo "${GH_EXISTING_ISSUE:-}";;
  "issue create") echo "https://github.com/x/y/issues/1";;
  *) :;;
esac
EOF
# npx node@<spec> -p process.execPath: print the path of a fake node for that
# spec, whose --version is "v<spec>"; NPX_FAIL=<spec> makes one unavailable.
cat > "$W/bin/npx" <<'NPX'
#!/usr/bin/env bash
echo "npx $(printf '%q ' "$@")" >> "$SHIM_LOG"
spec=""; for a in "$@"; do case "$a" in node@*) spec="${a#node@}";; esac; done
[ -n "$spec" ] || exit 2
[ "${NPX_FAIL:-}" = "$spec" ] && { echo "npm ERR! notarget node@$spec" >&2; exit 1; }
d="$SHIM_DIR/node-$spec/bin"; mkdir -p "$d"
printf '#!/usr/bin/env bash\n[ "$1" = --version ] && { echo "v%s"; exit 0; }\nexec %q "$@"\n' "$spec" "$(command -v node)" > "$d/node"
chmod +x "$d/node"; echo "$d/node"
NPX
chmod +x "$W/bin/npm" "$W/bin/gh" "$W/bin/npx"

git_q() { git -c init.defaultBranch=main -c user.name=t -c user.email=t@t "$@" >/dev/null 2>&1; }

# setup <name> <pkg_dir>: upstream repo (served as a file URL), fork clone with
# one source patch + CI on schuettc-publish, bare origin.
setup() {
  local name="$1" dir="$2" pj
  S="$W/$name"; rm -rf "$S"; mkdir -p "$S"
  pj="$S/up/${dir:+$dir/}package.json"
  mkdir -p "$(dirname "$pj")" && cd "$S/up" && git_q init
  printf '{\n  "name": "upstream-pkg",\n  "version": "1.0.0"\n}\n' > "$pj"
  printf 'line1\nline2\nline3\n' > "$S/up/${dir:+$dir/}src.txt"
  git_q add -A && git_q commit -m "up 1.0.0"
  git_q clone --bare "$S/up" "$S/origin.git"
  git_q clone "$S/origin.git" "$S/fork" && cd "$S/fork" && git_q checkout -b schuettc-publish
  printf 'line1\nOUR PATCH\nline3\n' > "${dir:+$dir/}src.txt"
  mkdir -p .github/scripts && cp "$SCRIPT" .github/scripts/upstream-sync.sh
  git_q add -A && git_q commit -m "our patch + CI" && git_q push origin schuettc-publish
}
upstream_bump() { # <dir> <version> <conflict:0|1>
  local dir="$1" ver="$2" conflict="$3" p
  cd "$S/up" || exit 1; p="${dir:+$dir/}"
  node -e 'const f=process.argv[1],p=JSON.parse(require("fs").readFileSync(f));p.version=process.argv[2];require("fs").writeFileSync(f,JSON.stringify(p,null,2)+"\n")' "${p}package.json" "$ver"
  if [ "$conflict" = 1 ]; then printf 'line1\nUPSTREAM CHANGE\nline3\n' > "${p}src.txt"; else echo "new" > "${p}other.txt"; fi
  git_q add -A && git_q commit -m "up $ver"
}
run() { # env... -> runs the script in the fork; sets RC and OUT
  export SHIM_LOG="$S/shim.log" SHIM_DIR="$S"; : > "$SHIM_LOG"; rm -f "$S/published-package.json" "$S/published-dist.js"
  cd "$S/fork" || exit 1
  OUT="$(env PATH="$W/bin:$PATH" GITHUB_REPOSITORY=schuettc/fork GITHUB_REPOSITORY_OWNER=schuettc \
      GITHUB_RUN_ID=1 UPSTREAM_REPO=ignored UPSTREAM_BRANCH=main PKG_NAME=@schuettc/pkg TAG_PREFIX=v \
      "$@" bash -c 'git remote add upstream "file://$UP_PATH" 2>/dev/null; sed "s#https://github.com/\${UPSTREAM_REPO}.git#file://$UP_PATH#g" .github/scripts/upstream-sync.sh > /tmp/us-$$.sh; bash /tmp/us-$$.sh; rc=$?; rm -f /tmp/us-$$.sh; exit $rc' 2>&1)"
  RC=$?
}
pub_field() { node -pe "JSON.stringify(require('$S/published-package.json')$1)"; }

for variant in root mono; do
  [ $variant = root ] && D="" || D="packages/pkg"
  PKG_DIR_ENV="${D:-.}"
  echo "== variant: $variant (PKG_DIR=$PKG_DIR_ENV)"

  setup "$variant-sync" "$D"; export UP_PATH="$S/up"
  run PKG_DIR="$PKG_DIR_ENV"
  check "in sync: exit 0, no publish" '[ $RC -eq 0 ] && grep -q "In sync" <<<"$OUT" && ! grep -q "npm publish" "$SHIM_LOG"'

  setup "$variant-clean" "$D"; export UP_PATH="$S/up"
  upstream_bump "$D" 1.1.0 0
  run PKG_DIR="$PKG_DIR_ENV" NPM_PUBLISHED='["1.1.0-schuettc.1"]'
  check "clean rebase: exit 0" '[ $RC -eq 0 ]'
  check "publishes next N (1.1.0-schuettc.2)" '[ "$(pub_field .version)" = "\"1.1.0-schuettc.2\"" ]'
  check "stamps scoped name" '[ "$(pub_field .name)" = "\"@schuettc/pkg\"" ]'
  check "stamps fork repo url" 'grep -q "github.com/schuettc/fork.git" <<<"$(pub_field .repository)"'
  if [ -n "$D" ]; then check "monorepo directory field" 'grep -q "packages/pkg" <<<"$(pub_field .repository)"'; fi
  check "branch keeps upstream name/version" '[ "$(git -C "$S/origin.git" show schuettc-publish:${D:+$D/}package.json | node -pe "JSON.parse(require(\"fs\").readFileSync(0)).name")" = "upstream-pkg" ]'
  check "branch pushed rebased (has upstream 1.1.0)" 'git -C "$S/origin.git" show schuettc-publish:${D:+$D/}package.json | grep -q 1.1.0'
  check "patch preserved on branch" 'git -C "$S/origin.git" show schuettc-publish:${D:+$D/}src.txt | grep -q "OUR PATCH"'
  check "tag pushed" 'git -C "$S/origin.git" rev-parse -q --verify refs/tags/v1.1.0-schuettc.2 >/dev/null'
  check "publishes with --ignore-scripts + provenance" 'grep -E "npm publish .*--provenance.*--ignore-scripts" "$SHIM_LOG" >/dev/null'

  setup "$variant-build" "$D"; export UP_PATH="$S/up"
  upstream_bump "$D" 1.4.0 0
  run PKG_DIR="$PKG_DIR_ENV" BUILD_CMD="mkdir -p ${D:-.}/dist && echo built > ${D:-.}/dist/index.js"
  check "BUILD_CMD output ships in the published copy" '[ $RC -eq 0 ] && grep -q built "$S/published-dist.js" 2>/dev/null'

  setup "$variant-testok" "$D"; export UP_PATH="$S/up"
  upstream_bump "$D" 1.5.0 0
  run PKG_DIR="$PKG_DIR_ENV" TEST_CMD="grep -q 'OUR PATCH' ${D:+$D/}src.txt"
  check "TEST_CMD passes: publishes" '[ $RC -eq 0 ] && [ -f "$S/published-package.json" ]'

  # current / lts resolve from nodejs.org's release index (NODE_DIST_INDEX in
  # tests): here current is 27 and the newest LTS 26. A major passes through.
  printf '[{"version":"v27.1.0","lts":false},{"version":"v26.5.0","lts":"K"},{"version":"v25.9.0","lts":false}]' > "$W/node-index.json"
  setup "$variant-testnodes" "$D"; export UP_PATH="$S/up"
  upstream_bump "$D" 1.5.1 0
  run PKG_DIR="$PKG_DIR_ENV" NODE_DIST_INDEX="file://$W/node-index.json" TEST_NODE_VERSIONS="current lts 24" TEST_CMD="node --version >> $S/ran-on"
  check "TEST_NODE_VERSIONS: TEST_CMD runs on the job's node, then each listed node (current, lts, a major)" '[ $RC -eq 0 ] && [ "$(sed -n 2,4p "$S/ran-on" | tr "\n" " ")" = "v27 v26 v24 " ] && [ "$(wc -l < "$S/ran-on")" -eq 4 ]'
  check "TEST_NODE_VERSIONS: publishes when every node passes" '[ -f "$S/published-package.json" ]'

  setup "$variant-testnodefail" "$D"; export UP_PATH="$S/up"
  upstream_bump "$D" 1.5.2 0
  run PKG_DIR="$PKG_DIR_ENV" NODE_DIST_INDEX="file://$W/node-index.json" TEST_NODE_VERSIONS="current" TEST_CMD='[ "$(node --version)" != v27 ]'
  check "TEST_NODE_VERSIONS: a failure on a listed node publishes nothing and names it" '[ $RC -ne 0 ] && ! grep -q "npm publish" "$SHIM_LOG" && grep -q "node@current" "$SHIM_LOG"'

  setup "$variant-testnodemissing" "$D"; export UP_PATH="$S/up"
  upstream_bump "$D" 1.5.3 0
  run PKG_DIR="$PKG_DIR_ENV" TEST_NODE_VERSIONS="99" NPX_FAIL=99 TEST_CMD=true
  check "TEST_NODE_VERSIONS: a node that cannot be fetched fails, publishes nothing" '[ $RC -ne 0 ] && ! grep -q "npm publish" "$SHIM_LOG"'

  setup "$variant-testnodebadindex" "$D"; export UP_PATH="$S/up"
  upstream_bump "$D" 1.5.4 0
  run PKG_DIR="$PKG_DIR_ENV" NODE_DIST_INDEX="file:///nonexistent" TEST_NODE_VERSIONS="current" TEST_CMD=true
  check "TEST_NODE_VERSIONS: current that cannot be resolved fails, publishes nothing" '[ $RC -ne 0 ] && ! grep -q "npm publish" "$SHIM_LOG"'

  setup "$variant-testfail" "$D"; export UP_PATH="$S/up"
  upstream_bump "$D" 1.6.0 0
  run PKG_DIR="$PKG_DIR_ENV" TEST_CMD="false"
  check "TEST_CMD fails: nonzero exit" '[ $RC -ne 0 ]'
  check "TEST_CMD fails: nothing published" '! grep -q "npm publish" "$SHIM_LOG"'
  check "TEST_CMD fails: issue names test phase" 'grep -q "phase.*test" "$SHIM_LOG"'
  check "TEST_CMD fails: branch not pushed" '! git -C "$S/origin.git" show schuettc-publish:${D:+$D/}package.json | grep -q 1.6.0'

  setup "$variant-conflict" "$D"; export UP_PATH="$S/up"
  upstream_bump "$D" 1.2.0 1
  run PKG_DIR="$PKG_DIR_ENV"
  check "conflict: exit 1" '[ $RC -eq 1 ]'
  check "conflict: nothing published" '! grep -q "npm publish" "$SHIM_LOG"'
  check "conflict: issue on the FORK (-R)" 'grep -E "gh issue create .*-R schuettc/fork" "$SHIM_LOG" >/dev/null'
  check "conflict: issue @mentions owner" 'grep -q "cc\\ @schuettc\|cc @schuettc" "$SHIM_LOG"'
  check "conflict: branch untouched" '! git -C "$S/origin.git" show schuettc-publish:${D:+$D/}package.json | grep -q 1.2.0'

  setup "$variant-pubfail" "$D"; export UP_PATH="$S/up"
  upstream_bump "$D" 1.3.0 0
  run PKG_DIR="$PKG_DIR_ENV" NPM_PUBLISH_FAIL=1
  check "publish failure: nonzero exit" '[ $RC -ne 0 ]'
  check "publish failure: issue names phase" 'grep -q "phase.*publish" "$SHIM_LOG"'
  check "publish failure: branch not pushed" '! git -C "$S/origin.git" show schuettc-publish:${D:+$D/}package.json | grep -q 1.3.0'
  check "publish failure: exactly ONE issue filed" '[ "$(grep -c "^gh issue create" "$SHIM_LOG")" = 1 ]'
  check "issue dedupe uses list, not search" '! grep -q -- "--search" "$SHIM_LOG"'

  setup "$variant-dedupe" "$D"; export UP_PATH="$S/up"
  upstream_bump "$D" 1.7.0 0
  run PKG_DIR="$PKG_DIR_ENV" NPM_PUBLISH_FAIL=1 GH_EXISTING_ISSUE=7
  check "existing open issue: comments, no new issue" 'grep -q "^gh issue comment 7" "$SHIM_LOG" && ! grep -q "^gh issue create" "$SHIM_LOG"'
done

echo "== dry_run"
setup dry-sync ""; export UP_PATH="$S/up"
run PKG_DIR=. DRY_RUN=true TEST_CMD="touch $S/tested"
check "dry run in sync: still tests + packs" '[ $RC -eq 0 ] && [ -f "$S/tested" ] && grep -q "npm pack --dry-run" "$SHIM_LOG"'
setup dry-ahead ""; export UP_PATH="$S/up"; upstream_bump "" 2.0.0 0
run PKG_DIR=. DRY_RUN=true
check "dry run: exit 0" '[ $RC -eq 0 ] && grep -q "DRY RUN OK: would publish @schuettc/pkg@2.0.0-schuettc.1" <<<"$OUT"'
check "dry run: packed copy is stamped" 'grep -q "@schuettc/pkg" "$S/packed-package.json"'
check "dry run: no publish" '! grep -q "npm publish" "$SHIM_LOG"'
check "dry run: branch not pushed, no tag" '! git -C "$S/origin.git" show schuettc-publish:package.json | grep -q 2.0.0 && ! git -C "$S/origin.git" rev-parse -q --verify refs/tags/v2.0.0-schuettc.1 >/dev/null'
setup dry-conflict ""; export UP_PATH="$S/up"; upstream_bump "" 2.1.0 1
run PKG_DIR=. DRY_RUN=true
check "dry run conflict: exit 1, no issue filed" '[ $RC -eq 1 ] && ! grep -q "gh issue" "$SHIM_LOG"'
setup dry-testfail ""; export UP_PATH="$S/up"
run PKG_DIR=. DRY_RUN=true TEST_CMD=false
check "dry run test failure: exit !=0, no issue filed" '[ $RC -ne 0 ] && ! grep -q "gh issue" "$SHIM_LOG"'

echo "== notify_test"
setup notify ""; export UP_PATH="$S/up"
run PKG_DIR=. NOTIFY_TEST=true
check "notify: exit 0" '[ $RC -eq 0 ]'
check "notify: create then close on fork" 'grep -q "gh issue create .*-R schuettc/fork" "$SHIM_LOG" && grep -q "gh issue close .*-R schuettc/fork" "$SHIM_LOG"'
check "notify: no fetch/publish" '! grep -q "npm publish" "$SHIM_LOG"'

echo "== --stamp (shared with first-publish.sh)"
sd="$(mktemp -d)"
printf '{"name":"up-name","version":"1.2.3","private":true,"repository":"x"}\n' > "$sd/package.json"
PKG_NAME=@schuettc/up-name VER=1.2.3-schuettc.1 FORK=schuettc/up-name DIR_FIELD=packages/up \
  bash "$SCRIPT_KIT" --stamp "$sd/package.json" >/dev/null 2>&1; rc=$?
check "stamp exits 0" '[ "$rc" = 0 ]'
check "stamp sets name" '[ "$(node -pe "require(\"$sd/package.json\").name")" = @schuettc/up-name ]'
check "stamp sets version" '[ "$(node -pe "require(\"$sd/package.json\").version")" = 1.2.3-schuettc.1 ]'
check "stamp sets repo + directory" '[ "$(node -pe "const r=require(\"$sd/package.json\").repository; r.url+\" \"+r.directory")" = "git+https://github.com/schuettc/up-name.git packages/up" ]'
check "stamp drops private" '[ "$(node -pe "String(require(\"$sd/package.json\").private)")" = undefined ]'
env -u VER PKG_NAME=@schuettc/up-name FORK=schuettc/up-name bash "$SCRIPT_KIT" --stamp "$sd/package.json" >/dev/null 2>&1; rc=$?
check "stamp without VER fails" '[ "$rc" != 0 ]'
rm -rf "$sd"

echo "== first-publish.sh"
setup fp "packages/pkg"
cd "$S/fork" || exit 1; mkdir -p .github/workflows
# A migrated fork carries only the caller: no .github/scripts/upstream-sync.sh.
git_q rm -r -q .github/scripts
cat > .github/workflows/upstream-sync.yml <<EOF
name: upstream-sync
jobs:
  sync:
    runs-on: ubuntu-26.04
    steps:
      - uses: schuettc/tools-actions/fork-sync@v0.1.0
        with:
          upstream_repo: "x/y"
          upstream_branch: "main"
          pkg_name: "@schuettc/pkg"
          pkg_dir: "packages/pkg"
          tag_prefix: "v"
          test_cmd: "touch $S/tested"
          build_cmd: "touch $S/built"
          force: \${{ inputs.force }}
EOF
node -e 'const f="packages/pkg/package.json",p=JSON.parse(require("fs").readFileSync(f));p.publishConfig={access:"public",provenance:true};require("fs").writeFileSync(f,JSON.stringify(p,null,2)+"\n")'
git_q add -A && git_q commit -m "ci workflow" && git_q push origin schuettc-publish
fp_run() { export SHIM_LOG="$S/shim.log" SHIM_DIR="$S"; : > "$SHIM_LOG"; rm -f "$S/packed-package.json" "$S/tested" "$S/built"
  OUT="$(env PATH="$W/bin:$PATH" FORK=schuettc/fork "$@" bash "$HERE/../first-publish.sh" "$S/fork" "$S/out" 2>&1)"; RC=$?; }
fp_run NPM_VIEW_404=1
packed() { node -pe "JSON.stringify(require('$S/packed-package.json')$1)"; }
check "first-publish exits 0" '[ "$RC" = 0 ]'
check "first-publish runs TEST_CMD and BUILD_CMD" '[ -f "$S/tested" ] && [ -f "$S/built" ]'
check "first-publish stamps name + -schuettc.1" '[ "$(packed .name)" = "\"@schuettc/pkg\"" ] && [ "$(packed .version)" = "\"1.0.0-schuettc.1\"" ]'
check "first-publish stamps the fork repo" '[ "$(packed .repository.url)" = "\"git+https://github.com/schuettc/fork.git\"" ]'
check "first-publish strips provenance, keeps access" '[ "$(packed .publishConfig)" = "{\"access\":\"public\"}" ]'
check "first-publish prints the publish command" 'echo "$OUT" | grep -q "npm publish .*--access public --ignore-scripts --tag latest"'
check "first-publish never publishes" '! grep -q "^npm publish" "$S/shim.log"'
fp_run
check "first-publish refuses an existing package" '[ "$RC" != 0 ] && echo "$OUT" | grep -q "already exists"'
cd "$S/fork" || exit 1; echo x >> packages/pkg/src.txt; fp_run NPM_VIEW_404=1; git_q checkout -- packages/pkg/src.txt
check "first-publish refuses a dirty tree" '[ "$RC" != 0 ] && echo "$OUT" | grep -q "working tree"'

echo; echo "passed $pass, failed $failn"
[ $failn -eq 0 ]
