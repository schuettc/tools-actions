# toolchain — one exact version everywhere, test what ships

A repo pins its Python and Node **once** — in `.python-version` and `.nvmrc` —
and every other place that names a version must defer to those files rather than
restate a version that then drifts. This action runs its bundled
`toolchain_consistency.py` against the calling repo's tree and **fails CI**
(exit 1) when any declared toolchain version disagrees: a Dockerfile base-image
`X.Y` that differs from `.python-version`, a base image that is not pinned by
digest (or is digest-pinned without a tag comment), an `ARG`-substituted `FROM`
that cannot be resolved, a `setup-python` step that does not read
`python-version-file: .python-version` (or that carries a `python-version:`
key), or a `setup-node` step that does not read `node-version-file: .nvmrc`.

The principle is **"one exact version everywhere, test what ships."** Every
discrepancy is reported with `file:line`; nothing silently skips — a configured
source that cannot be parsed is an error, and the check prints **every** finding
(it never stops at the first).

`toolchain_consistency.py` is **stdlib-only** and runs under the runner's own
`python3` (the action installs nothing); it requires **Python 3.12+** (tested on
3.12 and 3.14). The action embeds **no project facts**: where to look and what's
expected are the repo's own version files, read from the scanned tree — nothing
is hardcoded in the action.

## What it checks

Reading `.python-version` (its leading `X.Y`) and, when present, `.nvmrc` as the
sources of truth, it scans the tree and reports:

- **Dockerfiles** — any file named `Dockerfile`, `Dockerfile.*` or `*.Dockerfile`
  (excluding `.git`, `node_modules`, `.venv`, `cdk.out`, `.worktrees`;
  `docker-compose` files are **not** Dockerfiles and are ignored). For each real
  base image `FROM`:
  - a Python base whose `X.Y` differs from `.python-version`'s `X.Y`;
  - a base image not pinned by digest (`@sha256:`);
  - a digest-pinned base with **no tag comment** — one of the contiguous `#`
    lines immediately above the `FROM` must name that image's `path:tag` (this
    is also where the Python `X.Y` is read when the `FROM` carries only a
    digest);
  - an `ARG`-substituted `FROM` (`FROM …:${PY}`) — it **cannot** be resolved
    statically and fails loudly as `unresolvable` (never silently passes);
  - a Python base with no discernible version tag.
  - Stage-alias `FROM`s (`FROM base AS app`) and `FROM scratch` are resolved,
    not checked as base images.
- **Workflows** — `.github/workflows/*.yml`/`*.yaml` and
  `.github/actions/**/action.yml`:
  - a `setup-python` step missing `python-version-file: .python-version`, or
    carrying a `python-version:` key;
  - a `setup-node` step missing `node-version-file: .nvmrc` (only when `.nvmrc`
    exists).
- A **missing** `.python-version` (the repo must pin Python there).

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `root` | no | `.` | Repo root to scan. Every source of truth (`.python-version`, `.nvmrc`), every Dockerfile and every workflow under this root is read; nothing is hardcoded in the action. |

There is **no separate consumer config file**: the repo's own `.python-version`
and `.nvmrc` *are* the configuration — the single source of truth the rest of
the tree must agree with. That is the point of the action.

## A worked example

Given a repo with:

```
.python-version         # 3.14.7
.nvmrc                  # 24
packages/svc-a/Dockerfile
.github/workflows/ci.yml
```

where `packages/svc-a/Dockerfile` pins its base by digest with the tag
documented above it:

```dockerfile
# runtime base — pinned by DIGEST, tag documented below.
#   docker buildx imagetools inspect public.ecr.aws/lambda/python:3.14
FROM public.ecr.aws/lambda/python:3.14@sha256:721e972e16ef178662bae4b0185a062c959d020045e015bb565e9d6167ff565a
```

and `.github/workflows/ci.yml` reads the version files:

```yaml
- uses: actions/setup-python@v5
  with:
    python-version-file: .python-version
- uses: actions/setup-node@v4
  with:
    node-version-file: .nvmrc
```

the check exits `0`. Introduce a drift — say a Dockerfile pinned to
`lambda/python:3.12` while `.python-version` says `3.14`, and a workflow with an
inline `python-version: '3.11'` — and it exits `1` printing one line per
finding:

```
services/a.Dockerfile:2: base image is not pinned by digest (@sha256:): public.ecr.aws/lambda/python:3.12
services/a.Dockerfile:2: Python base image 3.12 differs from .python-version 3.14
.github/workflows/ci.yml:7: setup-python must not carry 'python-version:'; use 'python-version-file: .python-version'
.github/workflows/ci.yml:7: setup-python must set 'python-version-file: .python-version'
```

## Composing the pieces

Callers pin the action to an exact release tag (never a branch or `@main`). The
snippet below uses `ubuntu-26.04`; substitute your own exact runner label (never
a `-latest` label).

```yaml
jobs:
  toolchain:
    runs-on: ubuntu-26.04
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v7.0.1
      - uses: schuettc/tools-actions/toolchain@v0.9.2
```

To scan a subdirectory instead of the repo root, pass `root`:

```yaml
jobs:
  toolchain:
    runs-on: ubuntu-26.04
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v7.0.1
      - uses: schuettc/tools-actions/toolchain@v0.9.2
        with:
          root: services/api
```

## Pinning

Pin this action to an exact release tag, never a branch:

```yaml
- uses: schuettc/tools-actions/toolchain@v0.9.2
```
