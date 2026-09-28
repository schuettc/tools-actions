#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2034,SC2329
# (check() evals its single-quoted assertion later: SC2016 is intended, and the
# helpers/variables used only inside those strings trip SC2034/SC2329.)
# Contract tests for fork-sync/action.yml and the README caller snippet.
# Asserts, with yq (mikefarah v4) and actionlint:
#   - the interface: every input, which are required, and their defaults;
#   - every variable upstream-sync.sh marks required (`: "${X:?}"`) is mapped in
#     the run step's env:, and every input reaches the script unchanged;
#   - build_cmd maps straight through, so an empty build_cmd is an empty
#     BUILD_CMD, never "false" or unset (Review Focus 3);
#   - force/dry_run/notify_test are normalized to exactly "true"/"false", with a
#     schedule run (no dispatch inputs -> empty strings) resolving to "false"
#     (Review Focus 2);
#   - checkout fetches inputs.publish_branch with full history and the caller's
#     token, whatever ref the workflow was dispatched from (Review Focus 4);
#   - every `uses:` is an exact vX.Y.Z;
#   - the README caller snippet pins this repo's VERSION, passes exactly the
#     action's inputs, and is clean under actionlint.
# Usage: action.sh   (exit 0 = all assertions pass)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ACTION_DIR="$(cd "$HERE/.." && pwd)"
ROOT="$(cd "$ACTION_DIR/.." && pwd)"
ACTION="$ACTION_DIR/action.yml"
SCRIPT="$ACTION_DIR/upstream-sync.sh"
README="$ACTION_DIR/README.md"
SNIPPET_HEADING="## Caller workflow"

if ! { command -v yq >/dev/null && yq --version 2>&1 | grep -q 'version v4\.'; }; then
  echo "action.sh: needs mikefarah yq v4 on PATH" >&2; exit 2
fi
command -v actionlint >/dev/null || { echo "action.sh: needs actionlint on PATH" >&2; exit 2; }

pass=0; failn=0
ok()   { echo "  PASS $1"; pass=$((pass+1)); }
fail() { echo "  FAIL $1"; failn=$((failn+1)); }
check() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }
finish() { echo; echo "passed $pass, failed $failn"; [ "$failn" -eq 0 ]; exit $?; }

W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT

echo "== action.yml exists"
check "fork-sync/action.yml exists" '[ -f "$ACTION" ]'
[ -f "$ACTION" ] || finish

# q <expr> [file]: yq scalar lookup ("" for null)
q() { yq -r "$1 // \"\"" "${2:-$ACTION}"; }
RUN_STEP='.runs.steps[] | select((.run // "") | test("upstream-sync\.sh"))'
CHECKOUT='.runs.steps[] | select((.uses // "") | test("^actions/checkout@"))'
SETUP_NODE='.runs.steps[] | select((.uses // "") | test("^actions/setup-node@"))'
env_of() { yq -r "$RUN_STEP | .env.$1 // \"<unset>\"" "$ACTION"; }
has_env() { [ "$(yq -r "$RUN_STEP | .env | has(\"$1\")" "$ACTION")" = true ]; }

echo "== interface"
check "composite action" '[ "$(q .runs.using)" = composite ]'
for i in upstream_repo upstream_branch pkg_name pkg_dir tag_prefix test_cmd; do
  check "input $i is required" '[ "$(q ".inputs.$i.required")" = true ]'
done
check "input build_cmd defaults to \"\" (present, empty)" \
  '[ "$(yq -r ".inputs.build_cmd | has(\"default\")" "$ACTION")" = true ] && [ "$(yq -r ".inputs.build_cmd.default | tag" "$ACTION")" = "!!str" ] && [ -z "$(q .inputs.build_cmd.default)" ]'
for i in force dry_run notify_test; do
  check "input $i defaults to \"false\"" '[ "$(yq -r ".inputs.$i.default | tag" "$ACTION")" = "!!str" ] && [ "$(q ".inputs.$i.default")" = false ]'
done
check "input node_version defaults to \"22\"" '[ "$(yq -r ".inputs.node_version.default | tag" "$ACTION")" = "!!str" ] && [ "$(q .inputs.node_version.default)" = 22 ]'
check "input publish_branch defaults to schuettc-publish" '[ "$(q .inputs.publish_branch.default)" = schuettc-publish ]'
check "input token defaults to the caller's github.token" '[ "$(q .inputs.token.default)" = "\${{ github.token }}" ]'
for i in build_cmd force dry_run notify_test node_version publish_branch token; do
  check "input $i is optional" '[ "$(q ".inputs.$i.required")" != true ]'
done
check "no unexpected inputs" \
  '[ "$(yq -r ".inputs | keys | sort | join(\" \")" "$ACTION")" = "build_cmd dry_run force node_version notify_test pkg_dir pkg_name publish_branch tag_prefix test_cmd token upstream_branch upstream_repo" ]'

echo "== steps"
check "exactly one run step invokes upstream-sync.sh" '[ "$(yq -r "[$RUN_STEP] | length" "$ACTION")" = 1 ]'
check "script runs from \$GITHUB_ACTION_PATH" '[ "$(yq -r "$RUN_STEP | .run" "$ACTION" | tr -d "\n")" = "bash \"\$GITHUB_ACTION_PATH/upstream-sync.sh\"" ]'
check "every run step declares shell: bash" '[ "$(yq -r "[.runs.steps[] | select(has(\"run\")) | select(.shell != \"bash\")] | length" "$ACTION")" = 0 ]'
check "npm upgraded to latest before the script (OIDC needs npm >= 11.5.1)" \
  '[ "$(yq -r "[.runs.steps[] | select((.run // \"\") == \"npm install -g npm@latest\")] | length" "$ACTION")" = 1 ]'
check "step order: checkout, setup-node, npm upgrade, sync" \
  '[ "$(yq -r "[.runs.steps[] | (.uses // .run) | sub(\"@.*\", \"\") | sub(\"\n\$\", \"\")] | join(\"|\")" "$ACTION")" = "actions/checkout|actions/setup-node|npm install -g npm|bash \"\$GITHUB_ACTION_PATH/upstream-sync.sh\"" ]'

echo "== checkout (Review Focus 4)"
check "checkout ref is inputs.publish_branch" '[ "$(yq -r "$CHECKOUT | .with.ref" "$ACTION")" = "\${{ inputs.publish_branch }}" ]'
check "checkout fetch-depth is 0 (full history)" '[ "$(yq -r "$CHECKOUT | .with.fetch-depth" "$ACTION")" = 0 ]'
check "checkout uses the token input" '[ "$(yq -r "$CHECKOUT | .with.token" "$ACTION")" = "\${{ inputs.token }}" ]'
check "setup-node uses node_version and the npm registry" \
  '[ "$(yq -r "$SETUP_NODE | .with.node-version" "$ACTION")" = "\${{ inputs.node_version }}" ] && [ "$(yq -r "$SETUP_NODE | .with.registry-url" "$ACTION")" = "https://registry.npmjs.org" ]'

echo "== every uses: is an exact vX.Y.Z"
uses_list="$(yq -r '.. | select(tag == "!!map" and has("uses")) | .uses' "$ACTION")"
check "action.yml has uses: entries" '[ -n "$uses_list" ]'
while IFS= read -r u; do
  [ -n "$u" ] || continue
  check "pinned exactly: $u" '[[ "$u" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@v[0-9]+\.[0-9]+\.[0-9]+$ ]]'
done <<<"$uses_list"

echo "== script env (Review Focus 2, 3)"
# Top-level `: "${X:?}"` lines only: the indented one inside --stamp guards the
# stamp-only variables (VER, FORK), which the action never sets.
required="$(grep -E '^: ' "$SCRIPT" | grep -oE '\$\{[A-Z_]+:\?\}' | sed -E 's/^\$\{([A-Z_]+):\?\}$/\1/')"
check "found the script's required variables" '[ -n "$required" ]'
for v in $required; do
  check "required $v is set in the run step env" 'has_env "$v" && [ -n "$(env_of "$v")" ]'
done
for v in UPSTREAM_REPO UPSTREAM_BRANCH PKG_NAME PKG_DIR TAG_PREFIX TEST_CMD BUILD_CMD; do
  i="$(tr '[:upper:]' '[:lower:]' <<<"$v")"
  check "$v maps from inputs.$i unchanged" '[ "$(env_of "$v")" = "\${{ inputs.$i }}" ]'
done
check "GH_TOKEN is the token input" '[ "$(env_of GH_TOKEN)" = "\${{ inputs.token }}" ]'
check "BUILD_CMD is a bare pass-through (empty stays empty, never \"false\"/unset)" \
  '[ "$(env_of BUILD_CMD)" = "\${{ inputs.build_cmd }}" ] && ! grep -q "build_cmd.*||" <<<"$(env_of BUILD_CMD)"'

# Composite inputs are strings: a dispatch passes "true"/"false"; a schedule run
# has no dispatch inputs, so the caller's `${{ inputs.force }}` is "". The
# expression below is the one form that turns "true" into "true" and anything
# else (including "") into "false". Evaluate it for each case with the same
# semantics GitHub uses (== on strings is case-insensitive; && / || return
# operands) rather than trusting the string alone.
eval_flag() { # <expr> <input-value> -> what the script would receive, or "?" if unrecognized
  local expr="$1" val="$2" re="^\\\$\{\{ inputs\.([a-z_]+) == 'true' && 'true' \|\| 'false' \}\}$"
  [[ "$expr" =~ $re ]] || { echo "?"; return; }
  if [ "$(tr '[:upper:]' '[:lower:]' <<<"$val")" = true ]; then echo true; else echo false; fi
}
for pair in FORCE:force DRY_RUN:dry_run NOTIFY_TEST:notify_test; do
  v="${pair%%:*}"; i="${pair#*:}"
  e="$(env_of "$v")"
  check "$v is normalized from inputs.$i" '[ "$e" = "\${{ inputs.$i == '"'"'true'"'"' && '"'"'true'"'"' || '"'"'false'"'"' }}" ]'
  check "$v: schedule run (inputs.$i = \"\") -> false" '[ "$(eval_flag "$e" "")" = false ]'
  check "$v: dispatch true -> true" '[ "$(eval_flag "$e" true)" = true ]'
  check "$v: dispatch false -> false" '[ "$(eval_flag "$e" false)" = false ]'
done
check "run step env has no unexpected keys" \
  '[ "$(yq -r "$RUN_STEP | .env | keys | sort | join(\" \")" "$ACTION")" = "BUILD_CMD DRY_RUN FORCE GH_TOKEN NOTIFY_TEST PKG_DIR PKG_NAME TAG_PREFIX TEST_CMD UPSTREAM_BRANCH UPSTREAM_REPO" ]'

echo "== README caller snippet"
mkdir -p "$W/repo/.github/workflows"
snippet="$W/repo/.github/workflows/upstream-sync.yml"
[ -f "$README" ] && awk -v h="$SNIPPET_HEADING" '
  $0 == h { found = 1; next }
  found && !in_fence && /^```yaml[[:space:]]*$/ { in_fence = 1; next }
  in_fence && /^```[[:space:]]*$/ { exit }
  in_fence { print }' "$README" > "$snippet"
check "README has a yaml snippet under \"$SNIPPET_HEADING\"" '[ -s "$snippet" ]'
if [ -s "$snippet" ]; then
  version="$(tr -d '[:space:]' < "$ROOT/VERSION")"
  SNIP_STEP='.jobs[].steps[] | select((.uses // "") | test("^schuettc/tools-actions/fork-sync@"))'
  check "snippet pins schuettc/tools-actions/fork-sync@v$version (VERSION)" \
    '[ "$(yq -r "$SNIP_STEP | .uses" "$snippet")" = "schuettc/tools-actions/fork-sync@v$version" ]'
  want="$(yq -r '.inputs | to_entries | map(select(.value.required == true or (.key | test("^(build_cmd|force|dry_run|notify_test)$")))) | .[].key' "$ACTION" | sort | tr '\n' ' ')"
  got="$(yq -r "$SNIP_STEP | .with | keys | .[]" "$snippet" | sort | tr '\n' ' ')"
  check "snippet with: passes every required input plus build_cmd and the dispatch flags" '[ "$got" = "$want" ]'
  check "snippet forwards the dispatch inputs" \
    '[ "$(yq -r "$SNIP_STEP | .with.force" "$snippet")" = "\${{ inputs.force }}" ] && [ "$(yq -r "$SNIP_STEP | .with.dry_run" "$snippet")" = "\${{ inputs.dry_run }}" ] && [ "$(yq -r "$SNIP_STEP | .with.notify_test" "$snippet")" = "\${{ inputs.notify_test }}" ]'
  check "snippet grants contents/issues write and id-token write" \
    '[ "$(yq -r ".permissions.contents + \" \" + .permissions.issues + \" \" + .permissions[\"id-token\"]" "$snippet")" = "write write write" ]'
  check "snippet runs-on is exact (no -latest)" '! yq -r ".jobs[].\"runs-on\"" "$snippet" | grep -q -- "-latest"'
  [ -f "$ROOT/.github/actionlint.yaml" ] && cp "$ROOT/.github/actionlint.yaml" "$W/repo/.github/actionlint.yaml"
  lint_out="$(cd "$W/repo" && git init -q . && actionlint .github/workflows/upstream-sync.yml 2>&1)"; lint_rc=$?
  check "snippet passes actionlint" '[ "$lint_rc" = 0 ]'
  [ "$lint_rc" = 0 ] || printf '    %s\n' "$lint_out"
fi

finish
