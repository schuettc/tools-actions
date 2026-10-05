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

# A required gate must run every test. With a warm build cache Go replays a
# passing package's earlier result and prints "(cached)" instead of running it.
mkmod "$W/m" "$clean"
cat > "$W/m/cmd/x/main_test.go" <<'T'
package main

import "testing"

func TestPasses(t *testing.T) {}
T
run "$W/m"; run "$W/m"; rc=$?
check "a second run runs the tests again (no cached results)" '[ $rc -eq 0 ] && grep -q "^ok .*example.com/x/cmd/x" "$W/log" && ! grep -q "(cached)" "$W/log" || { grep "^ok" "$W/log"; false; }'

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

echo "== repo layouts the family uses"
# An npm dependency can ship Go source under node_modules; ./... would treat it
# as one of the repo's packages. It must not reach vet, test or build.
mkmod "$W/m" "$clean"; mkdir -p "$W/m/web/node_modules/dep/golang"
printf 'package dep\n\nfunc f() { notDefined() }\n' > "$W/m/web/node_modules/dep/golang/x.go"
run "$W/m"; rc=$?
check "Go source under node_modules is not vetted, tested or built" '[ $rc -eq 0 ] || { tail -3 "$W/log"; false; }'
# galley's layout: <repo>/.git is a bare repository and branches are worktrees
# under <repo>/.worktrees/. Go's VCS stamping fails there (exit status 128);
# the family stamps versions through ldflags, so the gate turns it off.
rm -rf "$W/bare"; mkdir -p "$W/bare"; mkmod "$W/bare" "$clean"
( cd "$W/bare" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm s \
  && git config core.bare true && rm -rf cmd go.mod && git worktree add -q .worktrees/br ) >/dev/null 2>&1
run "$W/bare/.worktrees/br"; rc=$?
check "a worktree of a bare repo builds (no VCS stamping)" '[ $rc -eq 0 ] || { tail -3 "$W/log"; false; }'
run "$W/m" PACKAGES=./cmd/...; rc=$?
check "an explicit PACKAGES list is used as given" '[ $rc -eq 0 ]'
# A library (tools-common) has no main package: go build -o <dir>/ refuses
# ("no main packages to build"), so it is built without -o, which compiles
# and discards. It must still be cross-built: a file that does not compile on
# one target fails the gate on that target.
rm -rf "${W:?}/lib"; mkdir -p "$W/lib"; printf 'module example.com/lib\n\ngo 1.22\n' > "$W/lib/go.mod"
printf 'package lib\n\n// F is exported.\nfunc F() int { return 1 }\n' > "$W/lib/lib.go"
run "$W/lib"; rc=$?
check "a library (no main package) passes and still cross-builds" '[ $rc -eq 0 ] && grep -q "build linux/amd64" "$W/log" || { tail -3 "$W/log"; false; }'
printf '//go:build linux\n\npackage lib\n\nfunc g() { notDefined() }\n' > "$W/lib/lib_linux.go"
run "$W/lib"; rc=$?
check "a library file that does not build on one target fails the cross-build" '[ $rc -ne 0 ] && grep -q "notDefined" "$W/log"'

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

echo "== the gate itself, with the real golangci-lint"
if [ -n "${REAL_GOLANGCI_LINT:-}" ]; then
  # Through ci.sh, not golangci-lint directly: the package list ci.sh hands the
  # linter must be one it can load. Import paths are not (it reports "0 issues"
  # after failing to type-check anything), so a planted finding must surface.
  mkmod "$W/r" 'package main

import (
	"errors"
	"io"
)

func g() error { return errors.New("x") }

func main() { _ = g() == io.EOF }'
  mkdir -p "$W/r/web/node_modules/dep"; printf 'package dep\n' > "$W/r/web/node_modules/dep/x.go"; git -C "$W/r" init -q
  ( cd "$W/r" && env PATH="$(dirname "$REAL_GOLANGCI_LINT"):$PATH" TARGETS=linux/amd64 RACE=false FAMILY_CONFIG="$A/golangci.yml" bash "$A/ci.sh" ) > "$W/log" 2>&1; rc=$?
  check "a real finding fails the gate and is reported" '[ $rc -ne 0 ] && grep -q "errorlint" "$W/log" || { tail -5 "$W/log"; false; }'
  mkmod "$W/r" "$clean"; mkdir -p "$W/r/internal/p"; printf 'package p\n\n// P is exported.\nfunc P() int { return 1 }\n' > "$W/r/internal/p/p.go"; git -C "$W/r" init -q
  ( cd "$W/r" && env PATH="$(dirname "$REAL_GOLANGCI_LINT"):$PATH" TARGETS=linux/amd64 RACE=false FAMILY_CONFIG="$A/golangci.yml" bash "$A/ci.sh" ) > "$W/log" 2>&1; rc=$?
  check "a clean multi-package module passes with no linter errors" '[ $rc -eq 0 ] && ! grep -q "level=error" "$W/log" || { tail -5 "$W/log"; false; }'
fi
# A linter that cannot load the code logs an error and still says "0 issues".
cat > "$W/bin/golangci-lint" <<FAKE
#!/usr/bin/env bash
echo "golangci-lint \$*" >> "$W/lint-calls"
case "\$1" in run) echo 'level=error msg="[linters_context] typechecking error: stat x: directory not found"'; echo "0 issues." ;; esac
exit 0
FAKE
mkmod "$W/m" "$clean"; run "$W/m"; rc=$?
check "a linter error fails the gate even when it reports 0 issues" '[ $rc -ne 0 ] && grep -qi "golangci-lint logged errors" "$W/log"'

echo; echo "passed $pass, failed $failn"
[ "$failn" -eq 0 ]
