# dependabot-stale — track Dependabot PRs held open too long

`dependabot-automerge` deliberately **holds major-version PRs** for a human to
review. Without a sweep, those held PRs rot silently: nobody is paged, the branch
drifts, and the "someone will look at it" never happens. This composite action is
that sweep. On a weekly `schedule` it lists open Dependabot PRs, and — for any
open longer than a threshold — keeps **one labelled tracking issue** that lists
them. When none remain stale, it closes the issue. It is the other half of the
auto-merge pair: automerge lands the safe bumps, this keeps the held ones from
going unattended.

It **never checks out or executes PR-controlled code** — it only reads PR
metadata via `gh` and manages a single issue.

## What it does

1. Validates its inputs (rejects an empty token, a non-positive `stale-days`, an
   empty `label` or `issue-title` — loudly, never a silent skip).
2. Lists open PRs authored by Dependabot (`dependabot[bot]` / `app/dependabot`)
   created before `now - stale-days`.
3. Ensures the tracking `label` exists (creates it, colour `d93f0b`, if absent).
4. If any PRs are stale: opens the tracking issue, or edits the existing one in
   place, with a title of `<issue-title> (N)` and a checklist body.
5. If none are stale: closes the open tracking issue (with a comment) if one
   exists; otherwise does nothing.

Every `gh` call is checked; a failure aborts the step loudly rather than being
swallowed. The label check captures `gh`'s output first, so a `gh` error is
distinguishable from the label merely being absent.

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `github-token` | yes | — | `GITHUB_TOKEN`, elevated by the caller's `permissions:`. Rejected loudly if empty. |
| `stale-days` | no | `14` | A Dependabot PR open longer than this many days is stale. Must be a positive integer. |
| `label` | no | `dependency-stale` | The fixed label on the single tracking issue. |
| `issue-title` | no | `Stale Dependabot PRs` | Base issue title; the current stale count is appended as ` (N)`. |

No calling-project fact (repo, org, branch) is baked in — the repo comes from
`github.repository` at run time.

## Caller example

The trigger and permissions live in the **caller**. Pair the weekly sweep with
the `pull_request` auto-merge job in one workflow, each job with its own
least-privilege `permissions:` block. Pin both actions to an exact release tag:

```yaml
name: Dependabot
on:
  pull_request:
    # Only PRs targeting the integration branch.
    branches: ["dev"]
  schedule:
    # Weekly sweep for held majors that have gone stale.
    - cron: "17 6 * * 1"
# No ambient permissions; each job grants only what it needs.
permissions: {}
jobs:
  automerge:
    name: Auto-merge allowed Dependabot PRs
    if: ${{ github.event_name == 'pull_request' && github.actor == 'dependabot[bot]' }}
    runs-on: ubuntu-26.04
    permissions:
      contents: write # enable auto-merge on the PR
      pull-requests: write # mark the PR for auto-merge
    steps:
      - uses: schuettc/tools-actions/dependabot-automerge@v0.10.0
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          target-branch: dev
  stale:
    name: Track stale Dependabot PRs
    if: ${{ github.event_name == 'schedule' }}
    runs-on: ubuntu-26.04
    permissions:
      contents: read
      pull-requests: read # list open Dependabot PRs
      issues: write # open / update / close the tracking issue
    steps:
      - uses: schuettc/tools-actions/dependabot-stale@v0.10.0
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          stale-days: "14"
```

`runs-on` is an exact image label (`ubuntu-26.04`), never a floating `*-latest`
alias.

## Security model

- **Least privilege.** The sweep needs `pull-requests: read` (list open PRs) and
  `issues: write` (manage the one tracking issue), plus `contents: read`. Under
  the top-level `permissions: {}` NOTHING is ambient, so the job must grant all
  three explicitly (the caller example does); no other scope is available.
- **No PR code.** It never checks out or runs PR-controlled code; it only reads
  metadata and edits an issue.
- **Loud on failure.** Every `gh` call is guarded; the step aborts on error
  rather than silently leaving held PRs untracked.
- **Exact pins.** Callers pin this action to an exact
  `schuettc/tools-actions/dependabot-stale@vX.Y.Z`.
