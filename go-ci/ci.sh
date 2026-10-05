#!/usr/bin/env bash
# The family Go gate, in order: gofmt, go vet, golangci-lint (config verified
# first, because a config golangci-lint cannot parse falls back silently and
# lints nothing), go test (-race, -count=1: never cached), and a CGO_ENABLED=0 build per release target.
#
# Env: PACKAGES (default ./..., minus anything under node_modules), RACE (true|false), TARGETS, FAMILY_CONFIG
#      (used when the repo has no .golangci.yml/.golangci.yaml), SKIP_LINT.
set -euo pipefail
# Versions are stamped through -ldflags, never by Go's VCS stamping, which
# fails in a worktree whose repository is bare (exit status 128).
export GOFLAGS="${GOFLAGS:+$GOFLAGS }-buildvcs=false"
pkgs="${PACKAGES:-./...}"
if [ "$pkgs" = ./... ]; then
  # ./... includes Go source an npm dependency ships under node_modules; that
  # is not the repo's code. List first (a failure here must fail the gate),
  # then filter. Relative directories, not import paths: golangci-lint takes
  # import paths as directories, fails to load them, and says "0 issues".
  listed="$(go list -e -f '{{.Dir}}' ./...)"
  pkgs="$(printf '%s\n' "$listed" | grep -v '/node_modules/' | sed "s#^$PWD\$#.#; s#^$PWD/#./#" || true)"
  [ -n "$pkgs" ] || { echo "::error::go-ci: no Go packages found"; exit 1; }
fi
step() { echo "::group::$1"; }
end() { echo "::endgroup::"; }

step gofmt
unformatted="$(find . -name '*.go' -not -path '*/node_modules/*' -not -path '*/.worktrees/*' \
  -not -path '*/vendor/*' -not -path './.git/*' -print0 | xargs -0 gofmt -l)"
if [ -n "$unformatted" ]; then
  echo "::error::go-ci: gofmt needed:"; echo "$unformatted"; exit 1
fi
end

step "go vet"
# shellcheck disable=SC2086  # PACKAGES is a list
go vet $pkgs || { echo "::error::go-ci: go vet failed"; exit 1; }
end

if [ "${SKIP_LINT:-false}" != true ]; then
  step golangci-lint
  config=""
  for c in .golangci.yml .golangci.yaml; do [ -f "$c" ] && { config="$c"; break; }; done
  config="${config:-${FAMILY_CONFIG:?}}"
  golangci-lint config verify --config "$config"
  # No caps: golangci-lint's defaults report at most 50 findings per linter
  # (and 3 of any one kind), which hid three quarters of a repo's findings.
  lintlog="$(mktemp)"
  lrc=0
  # shellcheck disable=SC2086  # PACKAGES is a list
  golangci-lint run --config "$config" --max-issues-per-linter=0 --max-same-issues=0 $pkgs 2>&1 | tee "$lintlog" || lrc=$?
  # A linter that cannot load the code logs level=error and can still report
  # "0 issues" and exit 0: a gate that stopped looking must not pass.
  if grep -q 'level=error' "$lintlog"; then
    rm -f "$lintlog"; echo "::error::go-ci: golangci-lint logged errors (above); it may not have linted the code"; exit 1
  fi
  rm -f "$lintlog"; [ "$lrc" -eq 0 ] || exit "$lrc"
  end
fi

step "go test"
race=(); [ "${RACE:-true}" = true ] && race=(-race)
# -count=1: a required gate must run every test. A warm build cache (setup-go's,
# or yours locally) enables Go's test-result cache, which replays a passing
# package's earlier result as "(cached)" instead of running its tests.
# shellcheck disable=SC2086
go test -count=1 ${race[@]+"${race[@]}"} $pkgs
end

step "cross-build"
out="$(mktemp -d)"; trap 'rm -rf "$out"' EXIT
# -o <dir>/ builds every main package into a scratch dir (never the tree), but
# refuses a package set with no main package ("no main packages to build"). A
# library is built without -o instead, which compiles every package and writes
# nothing, so it is cross-built all the same.
# shellcheck disable=SC2086
if [ -n "$(go list -f '{{if eq .Name "main"}}main{{end}}' $pkgs)" ]; then outflag=(-o "$out/"); else outflag=(); fi
for target in ${TARGETS:-darwin/arm64 darwin/amd64 linux/amd64 linux/arm64}; do
  echo "build $target"
  # shellcheck disable=SC2086
  CGO_ENABLED=0 GOOS="${target%/*}" GOARCH="${target#*/}" go build ${outflag[@]+"${outflag[@]}"} $pkgs
done
end
echo "go-ci: all checks passed"
