# go-ci

The `.tools` family Go gate, one step for every Go repo:

1. `gofmt` (ignores `node_modules`, `.worktrees`, `vendor`)
2. `go vet`
3. `golangci-lint` v2.14.0 (pinned in [`golangci-lint.lock`](golangci-lint.lock), checksum-verified). `config verify` runs first, because a config golangci-lint cannot parse falls back silently and lints nothing. The repo's `.golangci.yml` wins; otherwise the family default ([`golangci.yml`](golangci.yml): the standard linters).
4. `go test -race`
5. `CGO_ENABLED=0 go build` for darwin/linux × arm64/amd64

```yaml
jobs:
  ci:
    runs-on: ubuntu-26.04
    steps:
      - uses: actions/checkout@v7.0.1
      - uses: schuettc/tools-actions/go-ci@v0.9.3
```

## The same gate locally

`go-ci/local.sh` runs this gate on your machine at exactly the version your
`ci.yml` pins (it reads the `go-ci@vX.Y.Z` pin, fetches that version's gate,
family config and `golangci-lint.lock` into `~/.cache/tools-actions/`, and
installs the locked golangci-lint if yours differs). It also runs the Go that
CI's `actions/setup-go` installs from `go.mod`, not the Go on your PATH: the
`toolchain` directive if present, else the `go` directive, used as is when it
names a patch and resolved to that minor's newest stable patch (from go.dev,
cached for offline runs) when it does not. It sets `GOTOOLCHAIN` to that
version, so Go fetches it once; a `GOTOOLCHAIN` you set yourself is kept. Every repo's justfile
calls it through one standard recipe, so `just verify` and CI cannot disagree:

```just
# The family Go gate, at the tools-actions version ci.yml pins (same as CI).
gate:
    #!/usr/bin/env bash
    set -euo pipefail
    v="$(grep -oE 'go-ci@v[0-9]+\.[0-9]+\.[0-9]+' .github/workflows/ci.yml | head -1 | cut -d@ -f2)"
    f="${XDG_CACHE_HOME:-$HOME/.cache}/tools-actions/$v/go-ci/local.sh"
    [ -f "$f" ] || { mkdir -p "$(dirname "$f")"; curl -fsSL "https://raw.githubusercontent.com/schuettc/tools-actions/$v/go-ci/local.sh" -o "$f"; }
    bash "$f"
```

## Repo layouts the gate handles

- **Go source under `node_modules`** (an npm dependency can ship some) is left
  out of vet, lint, test and build when `packages` is `./...`.
- **Worktrees of a bare repository** (galley's layout) build: the gate sets
  `-buildvcs=false`, since versions are stamped through `-ldflags`.
- **A golangci-lint that logs an error fails the gate**, even if it reports
  "0 issues": it may not have loaded the code.
- **Locally, each checkout gets its own golangci-lint cache** (worktrees of one
  module otherwise report each other's findings). Set `GOLANGCI_LINT_CACHE` to
  override.

Inputs: `packages` (default `./...`), `race`, `targets`, `lint`, `setup-go`, `go-version-file`.
