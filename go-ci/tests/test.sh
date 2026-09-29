#!/usr/bin/env bash
# Tests for go-ci/ci.sh against tiny real Go modules. golangci-lint is a fake
# that records its arguments (the real one is exercised in each tool's CI).
#
# Usage: test.sh   (needs go; exit 0 = all pass)
# check() evaluates its condition later, so conditions are single-quoted on purpose.
# shellcheck disable=SC2016,SC2034  # ...and variables they read (rc) look unused to shellcheck
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
A="$(cd "$HERE/.." && pwd)"
pass=0; failn=0
ok()   { echo "  PASS $1"; pass=$((pass+1)); }
fail() { echo "  FAIL $1"; failn=$((failn+1)); }
check() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }
command -v go >/dev/null || { echo "test.sh: needs go" >&2; exit 2; }

W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
mkdir -p "$W/bin"
cat > "$W/bin/golangci-lint" <<FAKE
#!/usr/bin/env bash
echo "golangci-lint \$*" >> "$W/lint-calls"
exit 0
FAKE
chmod +x "$W/bin/golangci-lint"
export PATH="$W/bin:$PATH"

# mkmod <dir> <main.go body>: a module with one package.
mkmod() {
  rm -rf "$1"; mkdir -p "$1/cmd/x"
  printf 'module example.com/x\n\ngo 1.22\n' > "$1/go.mod"
  printf '%s\n' "$2" > "$1/cmd/x/main.go"
}
run() { ( cd "$1" && env TARGETS="linux/amd64 darwin/arm64" RACE=false FAMILY_CONFIG="$A/golangci.yml" "${@:2}" bash "$A/ci.sh" ) > "$W/log" 2>&1; }

clean='package main

import "fmt"

func main() { fmt.Println("ok") }'

echo "== a clean module passes every step"
mkmod "$W/m" "$clean"; : > "$W/lint-calls"
run "$W/m"; rc=$?
check "clean module passes" '[ $rc -eq 0 ]'
check "family config is used when the repo has none" 'grep -q -- "--config $A/golangci.yml" "$W/lint-calls"'
check "config is verified before linting" '[ "$(head -1 "$W/lint-calls")" = "golangci-lint config verify --config $A/golangci.yml" ]'
check "lint reports every finding (no per-linter cap)" 'grep -q "^golangci-lint run .*--max-issues-per-linter=0 --max-same-issues=0" "$W/lint-calls"'
check "cross-builds each target" 'grep -q "build linux/amd64" "$W/log" && grep -q "build darwin/arm64" "$W/log"'

echo "== the repo's own config wins"
mkmod "$W/m" "$clean"; printf 'version: "2"\n' > "$W/m/.golangci.yml"; : > "$W/lint-calls"
run "$W/m"
check "repo .golangci.yml is used" 'grep -q -- "--config .golangci.yml" "$W/lint-calls" && ! grep -q "$A/golangci.yml" "$W/lint-calls"'

echo "== failures"
mkmod "$W/m" 'package main
import "fmt"
func main() {   fmt.Println("x") }'
run "$W/m"; rc=$?
check "gofmt drift fails and names the file" '[ $rc -ne 0 ] && grep -q "cmd/x/main.go" "$W/log"'

mkmod "$W/m" 'package main

import "fmt"

func main() { fmt.Printf("%d\n", "not a number") }'
run "$W/m"; rc=$?
check "a vet error fails" '[ $rc -ne 0 ] && grep -qi "vet" "$W/log"'

mkmod "$W/m" "$clean"
cat > "$W/m/cmd/x/main_test.go" <<'T'
package main

import "testing"

func TestFails(t *testing.T) { t.Fatal("boom") }
T
run "$W/m"; rc=$?
check "a failing test fails" '[ $rc -ne 0 ] && grep -q "boom" "$W/log"'

mkmod "$W/m" "$clean"
mkdir -p "$W/m/node_modules/pkg" "$W/m/.worktrees/other"
printf 'package   bad\n' > "$W/m/node_modules/pkg/x.go"; printf 'package   bad\n' > "$W/m/.worktrees/other/x.go"
run "$W/m"; rc=$?
check "gofmt ignores node_modules and .worktrees" '[ $rc -eq 0 ]'

mkmod "$W/m" 'package main

func main() { undefinedCall() }'
run "$W/m"; rc=$?
check "a compile error fails" '[ $rc -ne 0 ] && grep -q "undefined" "$W/log"'
mkmod "$W/m" "$clean"; run "$W/m"
check "the build writes nothing into the tree" '[ ! -e "$W/m/x" ]'

echo "== the family config itself (real golangci-lint)"
if [ -n "${REAL_GOLANGCI_LINT:-}" ]; then
  # Branch work in this family happens in <repo>/.worktrees/<branch>. The
  # family config must lint such a checkout, not exclude it (it once matched
  # any path containing .worktrees and reported "0 issues" for everything).
  wt="$W/repo/.worktrees/br"; mkdir -p "$wt/cmd/x" "$wt/web/node_modules/q"
  printf 'module example.com/x\n\ngo 1.22\n' > "$wt/go.mod"
  printf 'package main\n\nimport "os"\n\nfunc main() { os.Remove("x") }\n' > "$wt/cmd/x/main.go"
  printf 'package q\n\nimport "os"\n\nfunc F() { os.Remove("x") }\n' > "$wt/web/node_modules/q/q.go"
  ( cd "$wt" && git init -q . && "$REAL_GOLANGCI_LINT" run --config "$A/golangci.yml" --max-same-issues=0 ./... ) > "$W/log" 2>&1
  check "lints a checkout that lives under .worktrees/" 'grep -q "cmd/x/main.go:5" "$W/log"'
  check "still excludes node_modules" '! grep -q "node_modules/q/q.go" "$W/log"'
  # The family set is stricter than golangci's standard: errorlint must run.
  printf 'package main\n\nimport (\n\t"errors"\n\t"io"\n)\n\nfunc g() error { return errors.New("x") }\n\nfunc h() bool { return g() == io.EOF }\n\nvar _ = h()\n' > "$wt/cmd/x/el.go"
  ( cd "$wt" && "$REAL_GOLANGCI_LINT" run --config "$A/golangci.yml" --max-same-issues=0 ./... ) > "$W/log" 2>&1
  check "the family set includes errorlint" 'grep -q "errorlint" "$W/log" || { sed "s/^/      | /" "$W/log" | tail -5; false; }'
else
  echo "  (skipped: set REAL_GOLANGCI_LINT to the pinned golangci-lint)"
fi

echo; echo "passed $pass, failed $failn"
[ "$failn" -eq 0 ]
