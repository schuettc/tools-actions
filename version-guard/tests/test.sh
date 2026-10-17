#!/usr/bin/env bash
# Tests for version-guard/guard.sh in throwaway git repos.
#
# Usage: test.sh   (exit 0 = all pass)
# check() evaluates its condition later, so conditions are single-quoted.
# shellcheck disable=SC2016,SC2034
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
G="$(cd "$HERE/.." && pwd)/guard.sh"
pass=0; failn=0
ok()   { echo "  PASS $1"; pass=$((pass+1)); }
fail() { echo "  FAIL $1"; failn=$((failn+1)); sed 's/^/      | /' "$W/log" | tail -3; }
check() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT

# repo <new VERSION or ""> : base commit with VERSION 1.0.0, head commit that
# changes VERSION (when given) and a README.
repo() {
  rm -rf "$W/r"; mkdir -p "$W/r"; cd "$W/r" || exit 1
  git init -q . && git config user.email t@t && git config user.name t
  printf '1.0.0\n' > VERSION; git add VERSION; git commit -qm base; BASE="$(git rev-parse HEAD)"
  echo x > README.md; [ -n "$1" ] && printf '%s\n' "$1" > VERSION
  git add -A; git commit -qm head; cd - >/dev/null || exit 1
}
# The guard reads the PR's current labels with `gh api`; a fake gh on PATH
# prints LABELS one per line (as --jq '.[].name' would), or fails when
# GH_FAIL is set.
mkdir -p "$W/bin"
cat > "$W/bin/gh" <<'EOF2'
#!/usr/bin/env bash
[ -n "${GH_FAIL:-}" ] && { echo "gh: HTTP 404" >&2; exit 1; }
printf '%s\n' $CURRENT_LABELS
EOF2
chmod +x "$W/bin/gh"
# run LABELS=<current labels> [EVENT_LABELS=<payload labels>] ...: the guard
# sees LABELS only through the fake gh; its own LABELS env is the event
# payload (EVENT_LABELS, default empty), which it must not trust.
run() {
  local cur="" ev="" a; local rest=()
  for a in "$@"; do case "$a" in LABELS=*) cur="${a#LABELS=}";; EVENT_LABELS=*) ev="${a#EVENT_LABELS=}";; *) rest+=("$a");; esac; done
  ( cd "$W/r" && env PATH="$W/bin:$PATH" BASE="$BASE" PR_NUMBER=7 REPO=o/r CURRENT_LABELS="$cur" LABELS="$ev" "${rest[@]}" bash "$G" ) > "$W/log" 2>&1
}

repo 1.0.1; run LABELS=""; rc=$?
check "a bumped VERSION passes and says old -> new" '[ $rc -eq 0 ] && grep -q "1.0.0 -> 1.0.1" "$W/log"'
repo ""; run LABELS=""; rc=$?
check "an unchanged VERSION fails" '[ $rc -ne 0 ] && grep -q "VERSION is unchanged" "$W/log"'
repo ""; run LABELS="docs no-release"; rc=$?
check "the no-release label skips the check" '[ $rc -eq 0 ] && grep -q "no-release" "$W/log"'
repo ""; run LABELS="no-release-please"; rc=$?
check "a label that merely contains no-release does not skip" '[ $rc -ne 0 ]'
repo 0.9.0; run LABELS=""; rc=$?
check "a VERSION that goes backwards fails" '[ $rc -ne 0 ] && grep -qi "not newer" "$W/log"'
repo "1.0.1-rc.1"; run LABELS=""; rc=$?
check "a pre-release VERSION fails (rcs come from dispatch, not VERSION)" '[ $rc -ne 0 ] && grep -qi "semver" "$W/log"'
repo "1.2"; run LABELS=""; rc=$?
check "a malformed VERSION fails" '[ $rc -ne 0 ] && grep -qi "semver" "$W/log"'
repo 1.10.0; run LABELS=""; rc=$?
check "numeric, not string, comparison (1.10.0 > 1.0.0)" '[ $rc -eq 0 ]'
repo ""; run LABELS="no-release" EVENT_LABELS=""; rc=$?
check "a label added after the PR opened counts (current labels, not the event payload)" '[ $rc -eq 0 ]'
repo ""; run LABELS="no-release" GH_FAIL=1; rc=$?
check "labels that can't be read fail loudly" '[ $rc -ne 0 ] && grep -qi "labels" "$W/log"'


echo; echo "passed $pass, failed $failn"
[ "$failn" -eq 0 ]
