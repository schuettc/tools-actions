# dependabot-automerge — land Dependabot PRs that meet a policy

Dependabot opens a stream of dependency PRs. The safe, low-noise ones — patch and
minor bumps, and docker digest refreshes — should land on their own the moment
the repo's required checks pass; the risky ones — majors — should wait for a
human. This composite action makes that happen: it reads Dependabot's own
metadata, applies a **policy you supply as inputs**, and, only for allowed
updates, arms **GitHub-native auto-merge** (`gh pr merge --auto`). The PR then
merges itself when — and only when — branch protection's required checks are
green.

It **never checks out or executes PR-controlled code**, and it re-checks that the
PR is Dependabot's own before doing anything. The tested policy decision lives in
`automerge_policy.py` beside `action.yml`, run via `$GITHUB_ACTION_PATH`.

## What it does

1. Validates its inputs (empty `github-token`, a bad `merge-method`, an empty
   `target-branch` fail loudly) and guards that the event is `pull_request` and
   the actor is `dependabot[bot]` (defense in depth — see the security model);
   on a non-PR event or a non-Dependabot actor it skips (exit 0), harmlessly.
2. Runs `dependabot/fetch-metadata@v2.5.0` (an exact pin) to read the PR's
   `update-type` and `package-ecosystem`.
3. Evaluates the policy: merge when the PR targets `target-branch`, its ecosystem
   is in `allowed-ecosystems`, and either the `update-type` is in
   `allowed-update-types` **or** it is an ungrouped docker **digest** bump and
   `allow-docker-digest` is true. fetch-metadata@v2.5.0 can't parse a digest
   bump's backticked commit message (`` Bumps node from `2fe369e` to `0e0ff40`. ``),
   so BOTH `update-type` and `new-version` come out empty; a digest bump is
   therefore recognised as `package-ecosystem == docker` + empty `update-type` +
   empty `new-version` **and** a PR title matching Dependabot's digest shape
   (`` Bump <image> from `<hex>` to `<hex>` ``). The title is read from the event
   payload via `env`, never interpolated into a shell. A PR against another
   branch, from a disallowed ecosystem, a major (absent from the default
   allow-list), or a docker change with an empty `update-type` that is NOT a
   recognisable digest bump (e.g. a `bookworm` -> `trixie` tag change) waits or
   fails loudly — never a silent merge. An empty or invalid
   `allowed-update-types` / `allowed-ecosystems` fails loudly too.
4. For an allowed PR, runs `gh pr merge --auto` with the chosen merge method.
   Never a direct merge — auto-merge respects the required-checks gate.

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `github-token` | yes | — | `GITHUB_TOKEN`, elevated by the caller's `permissions:`. Used by fetch-metadata and `gh pr merge`. Rejected loudly if empty. |
| `target-branch` | yes | — | The one branch Dependabot PRs may auto-merge into (the integration branch, e.g. `dev`). A PR whose base is not this branch is skipped. Rejected loudly if empty. |
| `merge-method` | no | `squash` | How auto-merge lands the PR: `squash`, `merge` or `rebase`. Validated up front. |
| `allowed-update-types` | no | `version-update:semver-patch version-update:semver-minor` | Space/comma-separated fetch-metadata update-types that may auto-merge. Add `version-update:semver-major` to auto-merge majors (they wait by default). Must be non-empty and every token must be `version-update:semver-{patch,minor,major}`. |
| `allowed-ecosystems` | no | `docker github-actions uv pip npm` | Space/comma-separated package-ecosystems whose PRs may auto-merge; any other ecosystem is skipped. Must not be empty. |
| `allow-docker-digest` | no | `true` | Auto-merge docker digest bumps (docker ecosystem with an empty update-type and empty new-version whose PR title matches Dependabot's `` Bump <image> from `<hex>` to `<hex>` `` shape). `true`/`false`. |

Everything is a **policy** input; no calling-project fact (repo, org, branch,
package, required-check name) is baked in. The required checks the merge waits on
are whatever the repo's branch protection defines — this action does not name
them.

## Required repo settings

- **Allow auto-merge** — Settings → General → Pull Requests → *Allow auto-merge*.
  Without it, `gh pr merge --auto` cannot arm and the step fails.
- **Required status checks** — a branch protection rule or ruleset on the target
  branch must define the checks the PR waits on. Auto-merge lands the PR only
  once every required check is green; with no required checks, an armed PR would
  merge immediately.

## Caller example

The triggers, permissions and actor gate live in the **caller** — they are
event/permission concerns GitHub only honours at the workflow/job level, and
keeping them here makes the security posture reviewable at a glance. Pair the
`pull_request` auto-merge job with the weekly `schedule` sweep
([`dependabot-stale`](../dependabot-stale/README.md)) in one workflow, each job
with its own least-privilege `permissions:` block. Pin both actions to an exact
release tag:

```yaml
name: Dependabot
on:
  pull_request:
    # Only PRs targeting the integration branch; the action also re-checks the
    # PR base against its target-branch input as defense in depth.
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
      - uses: schuettc/tools-actions/dependabot-automerge@v0.10.2
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          target-branch: dev
          merge-method: squash
          allowed-update-types: version-update:semver-patch version-update:semver-minor
          allowed-ecosystems: docker github-actions uv pip npm
          allow-docker-digest: "true"
  stale:
    name: Track stale Dependabot PRs
    if: ${{ github.event_name == 'schedule' }}
    runs-on: ubuntu-26.04
    permissions:
      contents: read
      pull-requests: read # list open Dependabot PRs
      issues: write # open / update / close the tracking issue
    steps:
      - uses: schuettc/tools-actions/dependabot-stale@v0.10.2
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          stale-days: "14"
```

`runs-on` is an exact image label (`ubuntu-26.04`), never a floating `*-latest`
alias.

Auto-merge deliberately **holds major-version PRs** for review; the weekly
`schedule` job is what keeps those held PRs from rotting silently. Run the pair
together — see [`dependabot-stale`](../dependabot-stale/README.md).

## `dependabot.yml` example

`dependabot.yml` is per-repo config, not part of this action — this is a
documented example only. It must match the action's policy for auto-merge to be
safe, so the example (and the test that guards it) keeps two things the source
guaranteed:

- **`target-branch` on every entry**, equal to the action's `target-branch` input
  (`dev` here). Without it Dependabot targets — and this action would auto-merge
  into — GitHub's default branch, not the integration branch.
- **a `cooldown` on every entry** (`default-days` ≥ 0), plus `semver-major-days`
  on the SemVer ecosystems (`uv`/`pip`/`npm`; docker and github-actions do not
  support it), so a freshly published release is not opened for auto-merge the
  instant it lands. This action does not read or enforce any cooldown; the delay
  exists only because your `dependabot.yml` sets it (as the example below does).
  Dependabot applies the cooldown before it opens the PR — the action only sees
  PRs Dependabot has already decided to open.

Group the SemVer ecosystems by `minor`/`patch` so one PR carries the safe bumps;
leave **docker ungrouped** (one PR per image) so a digest bump reports
single-dependency metadata with an empty `update-type` and empty `new-version`,
and a per-image PR title (`` Bump <image> from `<hex>` to `<hex>` ``) — which is
exactly what the digest rule keys on. Grouping docker would make fetch-metadata
report the group as a semver-major and defeat digest auto-merge:

```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    target-branch: "dev"
    schedule:
      interval: "weekly"
    cooldown:
      default-days: 3
      semver-major-days: 14
    groups:
      minor-and-patch:
        update-types:
          - "minor"
          - "patch"
  # docker is deliberately NOT grouped: one PR per image so a digest bump carries
  # an empty update-type. github-actions / docker do not support semver-*-days.
  - package-ecosystem: "docker"
    directory: "/"
    target-branch: "dev"
    schedule:
      interval: "weekly"
    cooldown:
      default-days: 3
  - package-ecosystem: "github-actions"
    directory: "/"
    target-branch: "dev"
    schedule:
      interval: "weekly"
    cooldown:
      default-days: 3
    groups:
      minor-and-patch:
        patterns:
          - "*"
```

For a private registry, add it under `registries:` in `dependabot.yml` and store
its credentials as **Dependabot** secrets (Settings → Secrets and variables →
Dependabot), not Actions secrets.

## Security model

- **`pull_request`, never `pull_request_target`.** A Dependabot-triggered
  `pull_request` run gets a read-only `GITHUB_TOKEN` by default; the caller's
  `permissions:` block elevates only what auto-merge needs. Because this action
  **never checks out or runs PR code**, `pull_request` with an elevated token is
  the safe choice; `pull_request_target` (which runs in the base-repo context
  with secrets) is unnecessary and riskier. On any event other than
  `pull_request` the action does not act — it skips cleanly (exit 0) rather than
  failing — so an accidentally broad trigger cannot auto-merge anything.
- **Only Dependabot's own PRs.** The caller gates the job with
  `if: github.actor == 'dependabot[bot]'`, and the action re-checks the actor as
  defense in depth: an accidentally ungated caller still cannot auto-merge a
  human's (or an attacker's) PR — it skips instead.
- **Native auto-merge, not a direct merge.** The action arms `gh pr merge
  --auto`; the PR lands only when the required checks pass. It never merges
  directly, so it can never bypass the gate.
- **Least privilege.** The job needs exactly `contents: write` (enable
  auto-merge) and `pull-requests: write` (mark the PR for auto-merge). The
  top-level workflow declares `permissions: {}` so no other scope is ambient.
- **Exact pins.** `dependabot/fetch-metadata` is pinned to `v2.5.0`; callers pin
  this action to an exact `schuettc/tools-actions/dependabot-automerge@vX.Y.Z`.

## Scope

This action ports the auto-merge policy. The reviewed source's other half — a
weekly *stale Dependabot PR* tracking job (one labelled issue listing PRs left
open past a threshold) — is a separate concern with its own permissions surface
(`issues: write`), shipped as the sibling action
[`dependabot-stale`](../dependabot-stale/README.md). Run the pair together: this
action lands the safe bumps, `dependabot-stale` keeps the held majors from
rotting silently.
