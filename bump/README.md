# bump — automated producer-first pin-bump chain

When a producer publishes a new version of a package, each consumer that pins
that package must move its pin, relock, and land the change — without a human
babysitting a PR. This action is the **consumer half** of that chain: it rewrites
the pin to the published version through the ONE shared matcher, relocks, opens a
PR into the integration branch, arms **GitHub-native auto-merge**, supersedes
older bump PRs, regenerates on a merge conflict, and opens a loud **stall issue**
on any un-merged ending (failure *or* cancellation). No bump stalls silently.

It ships two stdlib-only scripts beside `action.yml`, run under the runner's own
`python3` (the action installs no Python): **Python 3.12+** is required (tested on
3.12 and 3.14).

- `bump_pin.py` — the ONE pin rewriter. It preserves a `>=A,<B` range's ceiling,
  derives a next-major ceiling for an exact `==A` (dev-channel) pin, keeps a bare
  `>=A` floor's shape, and moves **every** file a package is pinned in. It also
  answers `--check` (is this repo a consumer?) and `stage` (stage exactly the
  bump's files, failing loudly on any stray path).
- `bump_flow.py` — the `gh`-only decision logic: **supersede** older open bump
  PRs (stepping aside for a newer one), **await** a PR to an outcome
  (merge / conflict / failure / timeout), **stall** (open or update one issue per
  package), and **resolve** (close that issue on merge).

## How it is wired

Consumers do **not** copy these scripts. They call the reusable workflow, which
calls this composite action at the same release tag:

```yaml
# .github/workflows/bump-lib.yml in the CONSUMER repo — the thin caller.
name: Bump lib
on:
  repository_dispatch:
    types: ["lib-published"] # the producer fires this after the wheel is in the index
  workflow_dispatch:
    inputs:
      package:
        description: "Package name to pin"
        required: true
        type: string
      version:
        description: "Version to pin (X.Y.Z) — must already exist in the index"
        required: true
        type: string

jobs:
  bump:
    uses: schuettc/tools-actions/.github/workflows/bump-pin.yml@v0.6.0
    with:
      package: ${{ github.event_name == 'workflow_dispatch' && inputs.package || github.event.client_payload.package }}
      version: ${{ github.event_name == 'workflow_dispatch' && inputs.version || github.event.client_payload.version }}
      check_consumer: false # single-producer: a missing pin STALLS, never a silent skip
      producer_release: "lib release"
      base_branch: dev
      app_client_id: ${{ vars.RELEASE_APP_CLIENT_ID }}
      runner: ubuntu-26.04 # an exact image label, never a *-latest alias
      private_index: "" # or the CodeArtifact JSON below
    secrets:
      app_private_key: ${{ secrets.RELEASE_APP_PRIVATE_KEY }}
      # index_role_arn: ${{ secrets.INDEX_ROLE_ARN }} # only with a private index
```

The reusable workflow owns the job shell — per-package concurrency
(`bump-<package>`), the write permissions, the App secret, the runner, and the
job-level stall step — and delegates every step to this composite action. The
stall step lives in the reusable workflow, **not** in the composite, because a
job-timeout cancellation does not run a composite action's post-failure steps;
keeping it at the job level is what makes "stall on cancellation" work.

### `check_consumer` defaults to false

This is a single-producer design: with `check_consumer: false`, a missing or
unrewritable pin **fails loudly to the stall issue**, never a silent green skip.
Set it `true` only for a genuine fan-out (a producer whose consumer may
legitimately not pin it yet).

## The consumer-owned `ci/bump/pins.toml`

Every project fact lives in this file; the scripts are verbatim across
consumers. The path is the `config` input (default `ci/bump/pins.toml`). The
scripts validate it on load — a missing, duplicate, empty or mistyped entry is a
loud error that names the offender.

| Key | Meaning |
| --- | --- |
| `lock_command` | Shell command to relock after the pin moves; `{package}` is substituted. `""` = no lock. |
| `post_lock_command` | Shell command run after the lock (e.g. an export script). `""` = none. |
| `stall_label` | The label carried by the one stall issue per package. |
| `stage_globs` | Paths/globs the lock and post-lock commands may change (the lock file plus any exported outputs). Pinned files are always allowed; **anything else the bump changed fails loudly**. |
| `[[package]]` `name` + `files` | Each consumed package and the file(s) its pin lives in. A package pinned in more than one file has every file rewritten. |

There is **no ecosystem name in the code** — a project that relocks with uv,
poetry, npm or anything else just lists the file(s) its command writes in
`stage_globs`.

```toml
lock_command = "uv lock --upgrade-package {package}"
post_lock_command = ""
stall_label = "chain-stall"
# The lock file the command writes, plus any exported outputs. A lock file NOT
# listed here is a stray change and fails the bump loudly (a visible
# misconfiguration, never a silent sweep).
stage_globs = ["uv.lock"]

[[package]]
name = "lib-one"
files = ["packages/app/pyproject.toml"]

[[package]]
name = "lib-two"
files = ["packages/app/pyproject.toml", "packages/lib/pyproject.toml"]
```

A public-index, no-relock project is simply:

```toml
lock_command = ""
post_lock_command = ""
stall_label = "chain-stall"
stage_globs = []

[[package]]
name = "lib-one"
files = ["pyproject.toml"]
```

### Pin shapes

The one matcher rewrites exactly three shapes and reports which it did:

- `>=A,<B` — the steady-state range. The floor moves to the new version; the
  **ceiling is preserved verbatim** (it tracks the wheel's major and moves by a
  deliberate PR, not by this bump). A two-component ceiling (`<2.0`) is legal and
  is not rewritten to three components.
- `==A` — the documented dev-channel form (`==X.Y.Z.devN`). It becomes a floor
  and a **derived** next-major ceiling (`<{major+1}.0.0`).
- `>=A` — a bare floor with no ceiling, deliberately. The floor moves and the
  shape is preserved; no ceiling is derived (that would silently narrow a bound
  nobody chose).

Any other pin string fails loudly, **naming what it found** rather than a bare
"could not find the pin".

## A private (CodeArtifact) index

Optional. When `private_index` is a CodeArtifact JSON, the action authenticates
via OIDC (using the `index_role_arn` secret), verifies `package==version` is in
the index before opening the PR, and exports a token for the relock:

```yaml
    with:
      # ...
      private_index: '{"kind":"codeartifact","domain":"my-domain","owner":"111111111111","repository":"my-py","region":"us-east-1"}'
    secrets:
      app_private_key: ${{ secrets.RELEASE_APP_PRIVATE_KEY }}
      index_role_arn: ${{ secrets.INDEX_ROLE_ARN }}
```

`private_index: ""` (the default) is a public index and skips all of the above.

## Required repo settings

- **Allow auto-merge** must be enabled in the consumer repo
  (Settings → General → Pull Requests → *Allow auto-merge*). The action arms
  `gh pr merge --auto --squash --delete-branch`; without the setting the PR never
  lands.
- **A GitHub App** with **Contents: R/W**, **Pull requests: R/W** and
  **Actions: R/W**, installed on the repo. Its client id goes in an Actions
  **variable** (passed as `app_client_id`) and its PEM private key in an Actions
  **secret** (passed as `app_private_key`). App-created PRs *do* trigger
  `pull_request` CI (a `GITHUB_TOKEN`-opened PR does not, by anti-recursion), so
  native auto-merge can land the bump. CI status is read with the workflow's own
  `GITHUB_TOKEN` — the App installation token cannot see the status rollup.
- A **required check named `CI`** on the base branch: the await loop treats a red
  `CI` run as a real failure (and stalls), while a non-required advisory workflow
  going red never stalls a bump.

## Pinning

Pin the reusable workflow (and, if you use it directly, this action) to an exact
release tag — never a branch or `@main`:

```yaml
- uses: schuettc/tools-actions/.github/workflows/bump-pin.yml@v0.6.0
```

```yaml
- uses: schuettc/tools-actions/bump@v0.6.0
```

Use an **exact runner label** (`ubuntu-26.04`), never a floating `*-latest`
alias.
