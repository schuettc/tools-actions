#!/usr/bin/env bash
# The family Go gate, in order: gofmt, go vet, golangci-lint (config verified
# first, because a config golangci-lint cannot parse falls back silently and
# lints nothing), go test (-race), and a CGO_ENABLED=0 build per release target.
#
# Env: PACKAGES (default ./...), RACE (true|false), TARGETS, FAMILY_CONFIG
#      (used when the repo has no .golangci.yml/.golangci.yaml), SKIP_LINT.
set -euo pipefail
pkgs="${PACKAGES:-./...}"
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
  # shellcheck disable=SC2086
  golangci-lint run --config "$config" $pkgs
  end
fi

step "go test"
race=(); [ "${RACE:-true}" = true ] && race=(-race)
# shellcheck disable=SC2086
go test ${race[@]+"${race[@]}"} $pkgs
end

step "cross-build"
out="$(mktemp -d)"; trap 'rm -rf "$out"' EXIT
for target in ${TARGETS:-darwin/arm64 darwin/amd64 linux/amd64 linux/arm64}; do
  echo "build $target"
  # -o <dir>/ builds every main package into a scratch dir (never the tree).
  # shellcheck disable=SC2086
  CGO_ENABLED=0 GOOS="${target%/*}" GOARCH="${target#*/}" go build -o "$out/" $pkgs
done
end
echo "go-ci: all checks passed"
