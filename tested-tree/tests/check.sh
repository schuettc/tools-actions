#!/usr/bin/env bash
# Verifies tested-tree/action.yml is byte-identical to the muda source
# recorded in SOURCE, and that both README.md workflow examples (the
# recording step a consumer keeps in its ci.yml, and the deploy-job example
# that calls this lookup) are clean under actionlint.
#
# Usage: check.sh   (exit 0 = both checks pass)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ACTION_DIR="$(cd "$HERE/.." && pwd)"
ROOT="$(cd "$ACTION_DIR/.." && pwd)"
ACTION="$ACTION_DIR/action.yml"
SOURCE="$ACTION_DIR/SOURCE"
README="$ACTION_DIR/README.md"

command -v actionlint >/dev/null || { echo "check.sh: needs actionlint on PATH" >&2; exit 2; }

pass=0; failn=0
ok()   { echo "  PASS $1"; pass=$((pass+1)); }
fail() { echo "  FAIL $1"; failn=$((failn+1)); }
finish() { echo; echo "passed $pass, failed $failn"; [ "$failn" -eq 0 ]; exit $?; }

W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT

echo "== action.yml matches muda source"
if [ ! -f "$ACTION" ]; then
  fail "tested-tree/action.yml exists"
elif [ ! -f "$SOURCE" ]; then
  fail "tested-tree/SOURCE exists"
else
  ok "tested-tree/action.yml exists"
  ok "tested-tree/SOURCE exists"
  want_sha="$(sed -n '2p' "$SOURCE" | tr -d '[:space:]')"
  got_sha="$(git hash-object "$ACTION")"
  if [ -n "$want_sha" ] && [ "$got_sha" = "$want_sha" ]; then
    ok "git hash-object tested-tree/action.yml == SOURCE line 2 ($want_sha)"
  else
    fail "git hash-object tested-tree/action.yml == SOURCE line 2 (want $want_sha, got $got_sha)"
  fi
fi

echo "== README examples pass actionlint"
if [ ! -f "$README" ]; then
  fail "tested-tree/README.md exists"
else
  ok "tested-tree/README.md exists"

  # Every fenced ```yaml block in README.md is a complete, standalone
  # workflow file; extract each in order and lint it on its own.
  awk '
    /^```yaml[[:space:]]*$/ { n++; file = "'"$W"'/block-" n ".yml"; next }
    /^```[[:space:]]*$/ { file = ""; next }
    file { print > file }
  ' "$README"
  blocks=()
  for b in "$W"/block-*.yml; do
    [ -e "$b" ] && blocks+=("$b")
  done

  if [ "${#blocks[@]}" -eq 0 ]; then
    fail "README.md has at least one \`\`\`yaml workflow example"
  else
    ok "README.md has ${#blocks[@]} \`\`\`yaml workflow example(s)"
    for b in "${blocks[@]}"; do
      [ -s "$b" ] || { fail "example $(basename "$b") is non-empty"; continue; }
      wdir="$W/repo-$(basename "$b" .yml)/.github/workflows"
      mkdir -p "$wdir"
      cp "$b" "$wdir/example.yml"
      [ -f "$ROOT/.github/actionlint.yaml" ] && cp "$ROOT/.github/actionlint.yaml" "$(dirname "$wdir")/actionlint.yaml"
      lint_out="$(cd "$(dirname "$(dirname "$wdir")")" && git init -q . && actionlint .github/workflows/example.yml 2>&1)"
      lint_rc=$?
      if [ "$lint_rc" = 0 ]; then
        ok "example $(basename "$b") passes actionlint"
      else
        fail "example $(basename "$b") passes actionlint"
        printf '    %s\n' "$lint_out"
      fi
    done
  fi
fi

finish
