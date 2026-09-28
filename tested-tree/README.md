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
- **The calling job must run `actions/checkout` first.** This is a composite
  action, resolved from the checked-out repo, and it computes the tree from
  that same checkout.

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
      - name: Record tested tree
        if: success()
        uses: actions/upload-artifact@v7.0.1
        with:
          name: tested-tree-${{ steps.tree.outputs.tree }}-${{ github.base_ref }}
          path: /dev/null
          retention-days: 7
```

- `retention-days` must be long enough that a record from a merged PR is
  still unexpired when the next deploy checks it — 7 days is a starting
  point, not a requirement.
- The artifact's content is irrelevant (the lookup checks its name only);
  `path: /dev/null` keeps it empty.
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
  actions: read

jobs:
  tested-tree:
    runs-on: ubuntu-26.04
    outputs:
      tested: ${{ steps.lookup.outputs.tested }}
    steps:
      - uses: actions/checkout@v7.0.1
      - id: lookup
        uses: schuettc/tools-actions/tested-tree@v0.1.0
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
      always() &&
      needs.tested-tree.result == 'success' &&
      (needs.check-a.result == 'success' || needs.check-a.result == 'skipped')
    runs-on: ubuntu-26.04
    steps:
      - run: ./deploy.sh
```

- `permissions.actions: read` is what lets `github.token` call the artifacts
  and runs APIs the lookup uses; no write scope is needed.
- `base:` is the branch being deployed — for a `deploy-dev` workflow it's
  `dev`, for `deploy-prod` it's `main` — and it must equal the `github.base_ref`
  the recording PR's CI run targeted.
- Each mirrored check job (`check-a` above; a real workflow may have several)
  is skipped with `if: needs.tested-tree.outputs.tested != 'true'`.
- `deploy` treats a skipped mirrored check the same as a passing one
  (`needs.check-a.result == 'skipped' || == 'success'`), and still requires
  the lookup job itself to have run successfully.

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
| `tests/check.sh` | Confirms `action.yml` still matches `SOURCE`'s sha, and lints both README examples above. |
