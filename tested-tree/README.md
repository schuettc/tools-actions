# tested-tree — skip mirrored checks a deploy already ran in CI

A deploy workflow often mirrors some of `ci.yml`'s check jobs so a merge to
`main`/a tag/a release branch is gated the same way a PR was. Re-running them
at deploy time is wasted work when nothing changed between the PR's merge
commit and the tree being deployed. This lookup answers one question: **did
this exact tree already pass this repo's `ci.yml` for a PR into this exact
base branch, successfully?** If yes, the deploy can skip its mirrored checks;
if there's any doubt, it can't.

See [`action.yml`](action.yml) for the full rationale (the tree-vs-commit
distinction, why the base branch is part of the key, and the fail-safe
design). The short version:

- **Fail-safe by construction.** Any inability to prove the tree passed CI for
  this base — API error, no record, wrong workflow, non-`pull_request` event,
  non-success conclusion, an expired artifact — outputs `tested=false`. A
  lookup that can't be verified never buys a skip.
- **Records are keyed `tested-tree-<tree>-<base>`.** `<tree>` is
  `git rev-parse HEAD^{tree}` of the checked-out commit; `<base>` is the PR's
  `github.base_ref`. CI config can legitimately differ by target branch, so a
  record from a PR into `dev` never satisfies a deploy to `main`, and
  vice-versa.
- **The calling job must run `actions/checkout` first.** The action computes
  `HEAD^{tree}` from the job's checkout; without one there's no tree to look
  up.

## The contract, both halves

### 1. Recording — kept in the consumer's `ci.yml`

After the PR's checks succeed, upload an empty artifact named
`tested-tree-<tree>-<base>`. The lookup checks the artifact's name, that the
recording run was `.github/workflows/ci.yml`, that its event was
`pull_request`, that its conclusion was `success`, and that the artifact
hasn't expired — so `retention-days` must cover the time between the PR
merging and the last deploy that might skip on it.

```yaml
name: ci

on:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-26.04
    steps:
      - uses: actions/checkout@v7.0.1
      - name: Run the checks
        run: ./run-checks.sh
      - name: Compute tree
        id: tree
        run: echo "tree=$(git rev-parse 'HEAD^{tree}')" >> "$GITHUB_OUTPUT"
      - name: Create the empty record file
        if: success()
        run: touch "$RUNNER_TEMP/tested-tree"
      - name: Record tested tree
        if: success()
        uses: actions/upload-artifact@v7.0.1
        with:
          name: tested-tree-${{ steps.tree.outputs.tree }}-${{ github.base_ref }}
          path: ${{ runner.temp }}/tested-tree
          if-no-files-found: error
          overwrite: true
          retention-days: 7
```

- `retention-days` must be long enough that a record from a merged PR is
  still unexpired when the next deploy checks it — 7 days is a starting
  point, not a requirement.
- The artifact's content is irrelevant (the lookup checks its name only), so
  the record is one empty file, `$RUNNER_TEMP/tested-tree`.
  `if-no-files-found: error` makes a missing file fail the step instead of
  silently recording nothing; `overwrite: true` lets a re-run of the same PR
  replace its earlier record.
- Artifact names can't contain `/`, so a `github.base_ref` like `release/x`
  makes the name invalid and the upload fails. The pattern assumes base
  branches without slashes, such as `dev` and `main`.
- This step must run only after the checks it stands in for succeeded
  (`if: success()`), on the `pull_request` event, from `ci.yml` at that exact
  path — the lookup rejects any other workflow, event, or conclusion.

### 2. Looking up — in the deploy workflow

The lookup runs as its own job, and every mirrored check job is gated on its
`tested` output:

```yaml
name: deploy-prod

on:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  tested-tree:
    runs-on: ubuntu-26.04
    permissions:
      contents: read
      actions: read
    outputs:
      tested: ${{ steps.lookup.outputs.tested }}
    steps:
      - uses: actions/checkout@v7.0.1
      - id: lookup
        uses: schuettc/tools-actions/tested-tree@v0.2.0
        with:
          base: main

  check-a:
    needs: tested-tree
    if: needs.tested-tree.outputs.tested != 'true'
    runs-on: ubuntu-26.04
    steps:
      - uses: actions/checkout@v7.0.1
      - run: ./run-checks.sh

  deploy:
    needs: [tested-tree, check-a]
    if: |
      !cancelled() &&
      needs.tested-tree.result == 'success' &&
      (needs.check-a.result == 'success' ||
        (needs.check-a.result == 'skipped' && needs.tested-tree.outputs.tested == 'true'))
    runs-on: ubuntu-26.04
    steps:
      - run: ./deploy.sh
```

- `actions: read` is what lets `github.token` call the artifacts and runs APIs
  the lookup uses; no write scope is needed. Grant it on the `tested-tree` job
  only. The workflow-level `permissions` stay at `contents: read`, which is all
  the check and deploy jobs need to check out.
- `base:` is the branch being deployed — for a `deploy-dev` workflow it's
  `dev`, for `deploy-prod` it's `main` — and it must equal the `github.base_ref`
  the recording PR's CI run targeted.
- Each mirrored check job (`check-a` above; a real workflow may have several)
  is skipped with `if: needs.tested-tree.outputs.tested != 'true'`.
- `deploy` states its gate explicitly: the run isn't cancelled
  (`!cancelled()`), the lookup job succeeded, and each mirrored check either
  succeeded or was skipped *because* the lookup said `tested == 'true'`. A
  check skipped for any other reason doesn't count as passing. With several
  check jobs, add one such clause per job, joined with `&&`.

## Inputs

| Input | Required | Meaning | Example |
|---|---|---|---|
| `base` | yes | Target branch being deployed; must equal the recording PR's `github.base_ref` | `main` |

## Outputs

| Output | Meaning |
|---|---|
| `tested` | `"true"` iff this exact tree already passed this repo's `ci.yml` for `base`; `"false"` on any failure to prove it |

## Files

| Path | Role |
|---|---|
| `action.yml` | The lookup: computes the checked-out tree, queries the artifacts and runs APIs, and emits `tested`. |
| `SOURCE` | The muda source this action is copied from, verbatim, and its git blob sha. |
| `tests/check.sh` | Confirms `action.yml` still matches `SOURCE`'s sha, that every `tested-tree@` pin in this README equals `v<VERSION>`, and lints both README examples above. |
