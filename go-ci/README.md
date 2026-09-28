# go-ci

The `.tools` family Go gate, one step for every Go repo:

1. `gofmt` (ignores `node_modules`, `.worktrees`, `vendor`)
2. `go vet`
3. `golangci-lint` v2.12.2 (pinned, checksum-verified). `config verify` runs first, because a config golangci-lint cannot parse falls back silently and lints nothing. The repo's `.golangci.yml` wins; otherwise the family default ([`golangci.yml`](golangci.yml): the standard linters).
4. `go test -race`
5. `CGO_ENABLED=0 go build` for darwin/linux × arm64/amd64

```yaml
jobs:
  ci:
    runs-on: ubuntu-26.04
    steps:
      - uses: actions/checkout@v7.0.1
      - uses: schuettc/tools-actions/go-ci@v0.2.2
```

Inputs: `packages` (default `./...`), `race`, `targets`, `lint`, `setup-go`, `go-version-file`.
