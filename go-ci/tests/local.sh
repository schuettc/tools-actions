#!/usr/bin/env bash
# Tests for go-ci/local.sh: it reads the go-ci pin from ci.yml, fetches that
# version's gate into a cache, and runs it. Fetches are served from a local
# directory via TOOLS_ACTIONS_BASE (file:// URL), so no network is used.
#
# Usage: local.sh   (exit 0 = all pass)
# check() evaluates its condition later, so conditions are single-quoted.
# shellcheck disable=SC2016,SC2034
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
A="$(cd "$HERE/.." && pwd)"
pass=0; failn=0
ok()   { echo "  PASS $1"; pass=$((pass+1)); }
fail() { echo "  FAIL $1"; failn=$((failn+1)); sed 's/^/      | /' "$W/log" 2>/dev/null | tail -4; }
check() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }

W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
# A fake published tools-actions: <base>/v9.9.9/go-ci/{ci.sh,golangci.yml}
mkdir -p "$W/pub/v9.9.9/go-ci"
cat > "$W/pub/v9.9.9/go-ci/ci.sh" <<'GATE'
#!/usr/bin/env bash
echo "gate v9.9.9 ran in $PWD with FAMILY_CONFIG=$FAMILY_CONFIG RACE=${RACE:-} TARGETS=${TARGETS:-}"
[ -f "$FAMILY_CONFIG" ] || { echo "no config"; exit 3; }
exit "${GATE_EXIT:-0}"
GATE
printf 'version: "2"\n' > "$W/pub/v9.9.9/go-ci/golangci.yml"
printf 'version 2.12.2\n' > "$W/pub/v9.9.9/go-ci/golangci-lint.lock"

repo="$W/repo"; mkdir -p "$repo/.github/workflows"
printf 'module example.com/x\n\ngo 1.26.3\n' > "$repo/go.mod"
cat > "$repo/.github/workflows/ci.yml" <<'YML'
jobs:
  ci:
    steps:
      - uses: schuettc/tools-actions/go-ci@v9.9.9
YML
mkdir -p "$W/bin"; printf '#!/bin/sh\necho "golangci-lint has version 2.12.2"\n' > "$W/bin/golangci-lint"; chmod +x "$W/bin/golangci-lint"
# -u GOTOOLCHAIN: CI's setup-go exports GOTOOLCHAIN=local, which local.sh would
# rightly keep; each case starts clean, as a developer's shell does.
run() { ( cd "$repo" && env -u GOTOOLCHAIN PATH="$W/bin:$PATH" TOOLS_ACTIONS_BASE="file://$W/pub" XDG_CACHE_HOME="$W/cache" "$@" bash "$A/local.sh" ) > "$W/log" 2>&1; }

echo "== runs the pinned version"
run; rc=$?
check "runs the gate at the ci.yml pin" '[ $rc -eq 0 ] && grep -q "gate v9.9.9 ran in $repo" "$W/log"'
check "passes the fetched family config" 'grep -q "FAMILY_CONFIG=$W/cache/tools-actions/v9.9.9/go-ci/golangci.yml" "$W/log"'
check "caches the fetched files" '[ -f "$W/cache/tools-actions/v9.9.9/go-ci/ci.sh" ]'

echo "== uses the cache"
rm -rf "$W/pub/v9.9.9"
run; rc=$?
check "a cached version runs offline" '[ $rc -eq 0 ] && grep -q "gate v9.9.9 ran" "$W/log"'

echo "== failures"
run GATE_EXIT=1; rc=$?
check "the gate's failure is the exit code" '[ $rc -eq 1 ]'
printf 'jobs: {}\n' > "$repo/.github/workflows/ci.yml"
run; rc=$?
check "no go-ci pin in ci.yml fails and says so" '[ $rc -ne 0 ] && grep -q "go-ci@" "$W/log"'
rm -rf "$repo/.github"
run; rc=$?
check "no ci.yml fails and says so" '[ $rc -ne 0 ] && grep -q "ci.yml" "$W/log"'
mkdir -p "$repo/.github/workflows"; printf '      - uses: schuettc/tools-actions/go-ci@v8.0.0\n' > "$repo/.github/workflows/ci.yml"
run; rc=$?
check "a version that cannot be fetched fails and names it" '[ $rc -ne 0 ] && grep -q "v8.0.0" "$W/log"'

echo "== golangci-lint: the pinned version, installed if needed"
os="$(uname -s | tr '[:upper:]' '[:lower:]')"; case "$(uname -m)" in arm64|aarch64) arch=arm64;; *) arch=amd64;; esac
printf '      - uses: schuettc/tools-actions/go-ci@v9.9.9\n' > "$repo/.github/workflows/ci.yml"
mkdir -p "$W/pub/v9.9.9/go-ci"
printf '#!/usr/bin/env bash\necho "gate ran with $(golangci-lint --version)"\n' > "$W/pub/v9.9.9/go-ci/ci.sh"
printf 'version: "2"\n' > "$W/pub/v9.9.9/go-ci/golangci.yml"
# A fake golangci-lint 2.12.2 release tarball and its checksum in the lock.
mkdir -p "$W/gcl/v2.12.2/golangci-lint-2.12.2-$os-$arch"
printf '#!/bin/sh\necho "golangci-lint has version 2.12.2"\n' > "$W/gcl/v2.12.2/golangci-lint-2.12.2-$os-$arch/golangci-lint"
chmod +x "$W/gcl/v2.12.2/golangci-lint-2.12.2-$os-$arch/golangci-lint"
( cd "$W/gcl/v2.12.2" && tar -czf "golangci-lint-2.12.2-$os-$arch.tar.gz" "golangci-lint-2.12.2-$os-$arch" )
sha="$( (shasum -a 256 "$W/gcl/v2.12.2/golangci-lint-2.12.2-$os-$arch.tar.gz" 2>/dev/null || sha256sum "$W/gcl/v2.12.2/golangci-lint-2.12.2-$os-$arch.tar.gz") | cut -d' ' -f1)"
printf 'version 2.12.2\n%s-%s %s\n' "$os" "$arch" "$sha" > "$W/pub/v9.9.9/go-ci/golangci-lint.lock"
printf '#!/bin/sh\necho "golangci-lint has version 2.9.0"\n' > "$W/bin/golangci-lint"
rm -rf "$W/cache"
run GOLANGCI_DL_BASE="file://$W/gcl"; rc=$?
check "a different golangci-lint on PATH: the pinned one is installed and used" '[ $rc -eq 0 ] && grep -q "gate ran with golangci-lint has version 2.12.2" "$W/log"'
printf '#!/bin/sh\necho "golangci-lint has version 2.12.2"\n' > "$W/bin/golangci-lint"; rm -rf "$W/cache/tools-actions/golangci-lint"
run GOLANGCI_DL_BASE="file:///nonexistent"; rc=$?
check "the right version on PATH is used as is (no download)" '[ $rc -eq 0 ] && grep -q "version 2.12.2" "$W/log"'
printf '#!/bin/sh\necho "golangci-lint has version 2.9.0"\n' > "$W/bin/golangci-lint"; rm -rf "$W/cache"
printf 'version 2.12.2\n%s-%s %s\n' "$os" "$arch" "0000000000000000000000000000000000000000000000000000000000000000" > "$W/pub/v9.9.9/go-ci/golangci-lint.lock"
run GOLANGCI_DL_BASE="file://$W/gcl"; rc=$?
check "a checksum mismatch fails and installs nothing" '[ $rc -ne 0 ] && grep -qi "checksum" "$W/log" && [ ! -e "$W/cache/tools-actions/golangci-lint/2.12.2/golangci-lint" ]'

echo "== golangci-lint cache per checkout"
# Worktrees of one module share import paths, so a shared golangci-lint cache
# reports findings from other worktrees' files. Each checkout gets its own.
printf '#!/usr/bin/env bash\necho "cache=$GOLANGCI_LINT_CACHE"\n' > "$W/pub/v9.9.9/go-ci/ci.sh"
printf '#!/bin/sh\necho "golangci-lint has version 2.12.2"\n' > "$W/bin/golangci-lint"
rm -rf "$W/cache"; printf 'version 2.12.2\n' > "$W/pub/v9.9.9/go-ci/golangci-lint.lock"
run; c1="$(grep -o 'cache=.*' "$W/log")"
mkdir -p "$W/repo2/.github/workflows"; cp "$repo/.github/workflows/ci.yml" "$W/repo2/.github/workflows/"; cp "$repo/go.mod" "$W/repo2/"
( cd "$W/repo2" && env PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" TOOLS_ACTIONS_BASE="file://$W/pub" bash "$A/local.sh" ) > "$W/log" 2>&1
c2="$(grep -o 'cache=.*' "$W/log")"
check "two checkouts get two lint caches, under the tools-actions cache" '[ -n "$c1" ] && [ "$c1" != "$c2" ] && case "$c1" in "cache=$W/cache/tools-actions/"*) true;; *) false;; esac'
( cd "$repo" && env GOLANGCI_LINT_CACHE=/mine PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" TOOLS_ACTIONS_BASE="file://$W/pub" bash "$A/local.sh" ) > "$W/log" 2>&1
check "a GOLANGCI_LINT_CACHE you set is kept" 'grep -q "cache=/mine" "$W/log"'

echo "== Go: the version CI's setup-go would use"
# CI installs Go from go.mod (actions/setup-go go-version-file): the toolchain
# directive if present, else the go directive, which is used as-is when it has
# a patch and resolved to that minor's newest patch when it does not. The local
# gate must run that Go, not whatever Go is on PATH.
printf '#!/usr/bin/env bash\necho "gotoolchain=${GOTOOLCHAIN:-unset}"\n' > "$W/pub/v9.9.9/go-ci/ci.sh"
rm -rf "$W/cache"
cat > "$W/godl.json" <<'JSON'
[{"version": "go1.27.1", "stable": true}, {"version": "go1.26.10", "stable": true},
 {"version": "go1.26.2", "stable": true}, {"version": "go1.26rc1", "stable": false}]
JSON
gomod() { printf 'module example.com/x\n\n%s\n' "$1" > "$repo/go.mod"; }
gomod 'go 1.26.3'
run GO_DL_JSON=/nonexistent; rc=$?
check "an exact go directive is used as is, without the network" '[ $rc -eq 0 ] && grep -q "gotoolchain=go1.26.3" "$W/log"'
gomod $'go 1.26\ntoolchain go1.26.5'
run GO_DL_JSON=/nonexistent; rc=$?
check "a toolchain directive wins over the go directive" '[ $rc -eq 0 ] && grep -q "gotoolchain=go1.26.5" "$W/log"'
gomod 'go 1.26'
run GO_DL_JSON="file://$W/godl.json"; rc=$?
check "a bare minor resolves to its newest patch (numeric, not lexical; no rc)" '[ $rc -eq 0 ] && grep -q "gotoolchain=go1.26.10" "$W/log"'
run GO_DL_JSON=file:///nonexistent; rc=$?
check "a resolved minor is cached, so later runs work offline" '[ $rc -eq 0 ] && grep -q "gotoolchain=go1.26.10" "$W/log"'
rm -rf "$W/cache/tools-actions/go"
run GO_DL_JSON=file:///nonexistent; rc=$?
check "an unresolvable minor with nothing cached fails and names it" '[ $rc -ne 0 ] && grep -q "1.26" "$W/log"'
gomod 'go 1.26.3'
run GOTOOLCHAIN=go1.99.0; rc=$?
check "a GOTOOLCHAIN you set is kept" '[ $rc -eq 0 ] && grep -q "gotoolchain=go1.99.0" "$W/log"'
rm "$repo/go.mod"
run; rc=$?
check "no go.mod fails and says so" '[ $rc -ne 0 ] && grep -q "go.mod" "$W/log"'
gomod 'go 1.26.3'

echo; echo "passed $pass, failed $failn"
[ "$failn" -eq 0 ]
