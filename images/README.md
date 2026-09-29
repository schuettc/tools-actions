# images — build container images once, promote by digest

CDK content-addresses container image assets: `cdk synth` writes
`cdk.out/*.assets.json`, and each `dockerImages.<id>` entry is a hash over its
staged context, Dockerfile, platform and build-secret *names*. This action runs
its bundled `images.py` against those manifests so an image is **built once
in CI**, verified, and **promoted dev→prod by digest** — never rebuilt for
prod. It drives `docker buildx` and `aws ecr`; it owns no AWS credentials (the
calling job configures its own role).

`images.py` is stdlib-only and runs under the runner's own `python3` (the action
installs nothing); it requires **Python 3.12+** (tested on 3.12 and 3.14). Every
project fact lives in a consumer-owned config file — the action embeds none.

## What it does

`images.py` has one subcommand per phase (all read `--cdk-out DIR` and
`--config PATH`):

- `list` — one JSON line per image asset (source + destination). No config
  needed.
- `build` — `docker buildx build` each asset with its manifest's context,
  Dockerfile, platform, build args, secrets and the derived registry
  layer-cache flags. `--mode push` tags `<registry>:<hash>` and pushes;
  `--mode load` builds locally only. `--mode push` **requires** `--registry`.
  `--cache none|read|readwrite` selects the cache mode. Attestations are off
  (`--provenance=false --sbom=false`) in both modes so PR verification builds
  the exact shape that ships.
- `assert-present` — the deploy guard: every asset's `<hash>` tag must exist in
  the target account's ECR repo, else exit 1 naming each missing
  `<stack>/<asset_id>:<hash>`.
- `digests` — one `{"hash","digest"}` line per asset.
- `promote` — copy each asset's `<hash>` image from the dev bootstrap repo to
  the prod one **by digest** (`docker buildx imagetools create
  --prefer-index=false`), never a rebuild. `--from-account`/`--to-account`
  default to `accounts.dev`/`accounts.prod`; when they are equal
  (single-account projects) promotion only verifies presence and copies
  nothing. On success it prints the verified `{hash: dev_digest}` map.
- `check-deployed` — post-deploy gate: each prod container Lambda's
  (`AWS::Lambda::Function` `Code.ImageUri`) or Batch job definition's
  (`AWS::Batch::JobDefinition`, `ContainerProperties` or `EcsProperties`)
  running image digest must equal the dev digest for its asset hash, via
  `--function-map`, else exit 1 with a per-resource report.

A `lambda` image must be a single image manifest (Lambda rejects an OCI index);
a `batch` image may be any shape. The rule is keyed on each image's config
`deploy_target`.

### `check-deployed` and Batch tags

Lambda reports a `Code.ResolvedImageUri` — the account resolves the tag to a
digest for you. **AWS Batch does not**: `describe-job-definitions` echoes the
image string verbatim and never resolves a tag. So `check-deployed` must resolve
a Batch image itself, and how it does that depends on the image's shape:

- `<repo>@sha256:…` (with or without a leading `:tag`) — the digest is used
  directly and must equal the expected (promoted) dev digest.
- `<repo>:tag` (a bare tag, no digest) — CDK's
  `ContainerImage.fromDockerImageAsset` renders `<bootstrap-repo>:<asset-hash>`.
  A tag is normally not a stable identity, but the CDK bootstrap
  `cdk-<qualifier>-container-assets-<account>-<region>` repo is created
  **IMMUTABLE**, so that tag is write-once and *is* a stable identity. The tag is
  accepted only if all three hold: (1) the repo's `imageTagMutability` is
  `IMMUTABLE` (or `IMMUTABLE_WITH_EXCLUSION` with no exclusion filter matching
  the tag), (2) the tag resolves to a digest via `ecr describe-images`, and
  (3) that digest equals the expected dev digest. Otherwise it fails loudly,
  naming the job definition, the image string and the reason — a mutable repo, a
  tag not found, or a digest mismatch.
- anything else (no tag and no digest, or a registry/account other than the
  expected one) fails.

The repo's mutability is read live at check time. Flipping the repo to `MUTABLE`
later does not retroactively re-point existing tags, but future tags could move,
so this gate runs on every deploy.

The account and region for the `ecr describe-repositories` / `ecr
describe-images` reads come from the config and the image's registry URI; they
are never hardcoded.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `command` | yes | — | `images.py` subcommand: `build`, `assert-present`, `promote`, `check-deployed`, `digests`, `list`. |
| `cdk-out` | yes | — | `cdk.out` directory holding the synthesized `*.assets.json` manifests. |
| `config` | no | `ci/images/images.toml` | Path to the consumer-owned `images.toml`. |
| `args` | no | `""` | Extra args passed **verbatim** to `images.py` (e.g. `--account`, `--mode`, `--cache`, `--registry`, `--function-map`). |
| `ecr-login-accounts` | no | `""` | Space-separated account ids to `docker login` to, each at its ECR registry in the config's `region`. Empty = no login. |
| `setup-buildx` | no | `"false"` | Set up `docker buildx` before running (needed for `build`). |

## Output

- `dev-digests` — the verified `{hash: dev_digest}` JSON map `promote` wrote
  (empty for other commands). Hand it to `check-deployed`.

### The `args` word-splitting note

`args` is passed **unquoted** so it word-splits into separate argv entries. Any
JSON argument — e.g. `check-deployed`'s `--function-map` — **must be compact**
(no spaces), which is exactly what `promote` emits for the `dev-digests` map. A
pretty-printed JSON blob would word-split into broken argv.

**Never pass `--github-output` in `args`.** The action wires it for `promote`
itself (inside the run step, where the shell expands `$GITHUB_OUTPUT`; a `with:`
value is not shell-expanded, so it would reach `images.py` as the literal string
`$GITHUB_OUTPUT`). Passing it anyway fails the step loudly.

## The consumer-owned `ci/images/images.toml`

Every project fact lives in this file; `images.py` is verbatim across
consumers. The config loader is the single validation point (there are no copier
validators): it fails loudly, naming the problem — a missing required key, a
value that is blank or the wrong type where a non-empty string is required
(`region`, `bootstrap_qualifier`, `accounts.dev`, `accounts.prod`), an account
id that is not 12 digits, a `cache_prefix` that is empty or uses invalid tag
characters while `cache_repo` is set, a bad image key, an ambiguous
`(dockerfile, target)` pair, or an out-of-set `deploy_target`.

Schema:

- `region` (required) — the AWS region the images live in.
- `bootstrap_qualifier` (required) — the CDK bootstrap qualifier (default
  `hnb659fds`).
- `cache_repo` (required, may be empty) — the ECR repo for the BuildKit layer
  cache; `""` disables caching (and any `--cache` other than `none` then fails
  loudly).
- `cache_prefix` (required) — a namespace prefixing each image's cache key. It
  may be empty only when `cache_repo` is empty; when `cache_repo` is set it must
  be a non-empty string of valid Docker tag characters (it heads the tag
  `<cache_prefix>-<key>`).
- `[accounts]` `dev` / `prod` (both required) — dev and prod account ids;
  `prod` may equal `dev` for single-account projects.
- `[[image]]` rows (at least one), each with:
  - `key` — a unique label matching `^[a-z0-9][a-z0-9-]*$`; the cache ref is
    `<dev_registry>/<cache_repo>:<cache_prefix>-<key>`.
  - `dockerfile` — the asset's Dockerfile path suffix, as it appears in
    `cdk.out` (matched by suffix).
  - `target` — the build target (`dockerBuildTarget`), or `""` when absent.
  - `deploy_target` — `lambda` or `batch`.

Images are matched on the **(dockerfile suffix, build target)** pair: zero
matches raises naming both; more than one raises `ambiguous` naming the
candidate keys (there is no first-match-wins). So two images built from the
*same* Dockerfile with different `--target`s map to distinct entries with
distinct cache keys. Ambiguous entries are rejected at load using the **same
suffix-match rule** as the runtime matcher, so `Dockerfile` and
`docker/Dockerfile` at the same target (both matchable by one asset) fail at
load, not only at run time.

### A full worked example

```toml
region = "us-east-1"
bootstrap_qualifier = "hnb659fds"
cache_repo = "build-cache"   # "" = no layer cache
cache_prefix = "my-repo"     # namespaces each image's cache key

[accounts]
dev = "111111111111"
prod = "222222222222"        # may equal dev (single-account projects)

# One Dockerfile, two Lambda images distinguished only by build target.
[[image]]
key = "light"
dockerfile = "infra/docker/Dockerfile.lambda"
target = "light"
deploy_target = "lambda"

[[image]]
key = "conform"
dockerfile = "infra/docker/Dockerfile.lambda"
target = "conform"
deploy_target = "lambda"

# A Batch worker image (any manifest shape is allowed).
[[image]]
key = "batch"
dockerfile = "services/batch/Dockerfile"
target = "app"
deploy_target = "batch"
```

## Composing the pieces

Callers pin the action to an exact release tag (never a branch or `@main`). The
snippets below use `ubuntu-26.04`; substitute your own exact runner label (never a `-latest` label).

**PR CI — verify images (build + smoke, no push):**

```yaml
jobs:
  verify-images:
    runs-on: ubuntu-26.04
    steps:
      - uses: actions/checkout@v7.0.1
      - uses: schuettc/tools-actions/images@v0.9.3
        with:
          command: build
          cdk-out: cdk.out
          setup-buildx: "true"
          args: --mode load --cache read
```

**deploy-dev — build/push, then guard every hash is present:**

```yaml
jobs:
  build-images:
    runs-on: ubuntu-26.04
    steps:
      - uses: actions/checkout@v7.0.1
      - uses: schuettc/tools-actions/images@v0.9.3
        with:
          command: build
          cdk-out: cdk.out
          setup-buildx: "true"
          ecr-login-accounts: "111111111111"
          args: --mode push --cache readwrite --registry 111111111111.dkr.ecr.us-east-1.amazonaws.com/cdk-hnb659fds-container-assets-111111111111-us-east-1
      - uses: schuettc/tools-actions/images@v0.9.3
        with:
          command: assert-present
          cdk-out: cdk.out
          ecr-login-accounts: "111111111111"
          args: --account 111111111111
```

**deploy-prod — promote by digest, guard, then check the deployed digests.**
`promote` emits a compact `dev-digests` JSON map; hand it to `check-deployed`
verbatim (it is word-split, so it must stay compact). The action wires
`promote`'s `--github-output "$GITHUB_OUTPUT"` itself — do **not** pass it via
`args`:

```yaml
jobs:
  promote:
    runs-on: ubuntu-26.04
    steps:
      - uses: actions/checkout@v7.0.1
      - id: promote
        uses: schuettc/tools-actions/images@v0.9.3
        with:
          command: promote
          cdk-out: cdk.out
          ecr-login-accounts: "111111111111 222222222222"
      - uses: schuettc/tools-actions/images@v0.9.3
        with:
          command: assert-present
          cdk-out: cdk.out
          ecr-login-accounts: "222222222222"
          args: --account 222222222222
      - uses: schuettc/tools-actions/images@v0.9.3
        with:
          command: check-deployed
          cdk-out: cdk.out
          args: --function-map ${{ steps.promote.outputs.dev-digests }}
```

## Pinning

Pin this action to an exact release tag, never a branch:

```yaml
- uses: schuettc/tools-actions/images@v0.9.3
```
