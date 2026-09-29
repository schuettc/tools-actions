# fork-sync — the standard for `@schuettc/*` republished forks

When we need a change in a third-party package before upstream merges it, we
fork, patch, and republish under our npm scope. This composite action is the
single source of truth for how that works. It exists because the first
generation of forks each grew their own sync automation and all of them failed
silently (see tools-ops
`docs/superpowers/specs/2026-09-22-fork-publish-sync-audit.md`).

A fork carries **only a pinned caller**, `.github/workflows/upstream-sync.yml`,
which calls `schuettc/tools-actions/fork-sync@vX.Y.Z`. The sync script
([`upstream-sync.sh`](upstream-sync.sh)) lives here, beside
[`action.yml`](action.yml), and runs from `$GITHUB_ACTION_PATH`. Dependabot
bumps the pin in each fork when this repo releases.

> **Operating a fork at your machine** (a failure email, a catch-up rebase, local
> tests, the npm account steps, pinning): see [`RUNBOOK.md`](RUNBOOK.md).

## Naming

- **`@schuettc/<name>`** is only for our patched builds of **someone else's**
  package. Upstream owns `<name>`; the scope marks it as our fork.
- **Packages we own are published unscoped** (`pi-bang`, `pi-wakeup`,
  `pi-tmux-bridge`, ...). Never put an original package under `@schuettc`.

## The model

- **`schuettc-publish`** is the fork's default branch = upstream + our genuine
  source patches + the caller workflow (and its Dependabot config). It carries
  **no packaging metadata**: upstream's `name`, `version`, `repository`,
  lockfile and changelog stay untouched, so upstream's every-release churn
  never conflicts with us.
- **`upstream-sync`** (daily, and on demand) checks out `schuettc-publish` with
  full history, rebases onto upstream, runs `test_cmd`, runs `build_cmd`,
  copies the package to an isolated directory, stamps the scoped name /
  `<upstream-version>-schuettc.N` / repo URLs there, and publishes via **npm
  OIDC trusted publishing** with provenance. Then it force-pushes the rebased
  branch and tags `<tag_prefix><version>`.
- **Anything wrong fails loud**: a rebase conflict, a test failure, a build or
  publish error → the run goes red and a deduplicated tracking issue is filed
  on the fork, @mentioning the owner (GitHub emails it).
- **The trusted-publisher claim names the caller.** npm sees the workflow that
  ran, i.e. the fork's `.github/workflows/upstream-sync.yml`, not this action,
  so the publisher configured on npmjs.com stays `upstream-sync.yml`.
- **kempt pins exact versions.** A publish does not change what runs; bump the
  pin in `dotfiles/kempt.toml` when you want the new version, then `kempt update`.
- **Alias pins, when other packages import the original name.** If other
  extensions import the upstream package *by name* (`@gotgenes/pi-permission-system`
  is imported by pi-auto-review and pi-hail), install the fork under that name
  with an npm alias instead of changing every consumer:
  `npm:<upstream-name>@npm:<@schuettc/name>@<version>`. pi and kempt pass the
  spec through to `npm install`; the fork lands at
  `node_modules/<upstream-name>` (the only copy), consumers' imports resolve to
  it, and the upstream package can't also be loaded (same path). Retiring is
  one string: pin the upstream spec again. Peer ranges don't enforce the fork
  (pi installs with `--legacy-peer-deps`, and `x.y.z-schuettc.N` satisfies no
  range), so a consumer that needs a fork-only capability checks for it at
  runtime.
- **Only `upstream-sync` runs unattended on a fork.** Upstream's scheduled,
  tag-triggered and publish workflows are disabled with `gh workflow disable`.

## Caller workflow

Each fork's `.github/workflows/upstream-sync.yml` is exactly this, with its own
values in `schedule` and `with:` (the example values are for a made-up
`example-org/widget`):

```yaml
name: upstream-sync
on:
  schedule:
    - cron: "29 9 * * *"
  workflow_dispatch:
    inputs:
      force: { type: boolean, default: false, description: "Publish the current state even if already in sync" }
      dry_run: { type: boolean, default: false, description: "Run rebase/test/build/pack end to end; publish, push and issue nothing" }
      notify_test: { type: boolean, default: false, description: "Open and close a test issue to prove alerts reach you, then stop" }
permissions: { contents: write, issues: write, id-token: write }
concurrency: { group: upstream-sync, cancel-in-progress: false }
jobs:
  sync:
    runs-on: ubuntu-26.04
    steps:
      - uses: schuettc/tools-actions/fork-sync@v0.9.3
        with:
          upstream_repo: "example-org/widget"
          upstream_branch: "main"
          pkg_name: "@schuettc/widget"
          pkg_dir: "."
          tag_prefix: "v"
          test_cmd: "npm ci && npm test"
          build_cmd: ""
          force: ${{ inputs.force }}
          dry_run: ${{ inputs.dry_run }}
          notify_test: ${{ inputs.notify_test }}
```

What each fork fills in:

- **`cron`**: `"<minute> 9 * * *"`, daily at 09:`<minute>` UTC. Give each fork its
  own minute so the forks don't all run at once.
- **`upstream_repo`** / **`upstream_branch`**: what we track, `owner/name` and a
  branch (`main`, `master`, ...).
- **`pkg_name`**: the published name, `@schuettc/<upstream-name>`.
- **`pkg_dir`**: the package directory relative to the repo root, `.` for a
  root package, `packages/<pkg>` in a monorepo.
- **`tag_prefix`**: the release tag prefix, `v` or `<pkg>-v` in a monorepo.
- **`test_cmd`**: the fork's gate (see Inputs).
- **`build_cmd`**: the build, or `""` when the package ships no built output.

Keep the rest verbatim:

- the dispatch inputs, forwarded as `${{ inputs.* }}`. On a schedule run they're
  empty, and the action treats empty as `false`.
- the permissions: `contents: write` to push the branch and tags,
  `issues: write` for the alerts, `id-token: write` for npm OIDC.
- the concurrency group, so two runs never race on one branch.
- an exact runner label.
- the exact `@vX.Y.Z` pin, never a branch or `@main`.

## Inputs

| Input | Required | Default | Meaning | Example |
|---|---|---|---|---|
| `upstream_repo` | yes | | upstream `owner/name` | `pungggi/pi-schedule` |
| `upstream_branch` | yes | | upstream branch to track | `master` |
| `pkg_name` | yes | | published name | `@schuettc/pi-schedule` |
| `pkg_dir` | yes | | package dir (`.` for root) | `extensions/pi-usage` |
| `tag_prefix` | yes | | release tag prefix | `v`, `pi-auto-review-v` |
| `test_cmd` | yes | | installs its own deps; gates publishing | `npm ci && npm run typecheck && npm test` |
| `build_cmd` | no | `""` | only if the package ships built output | `npm install && npm run build -w extensions/pi-usage` |
| `force` | no | `"false"` | `"true"` publishes even when already in sync | `${{ inputs.force }}` |
| `dry_run` | no | `"false"` | `"true"` runs everything but publish, push, tag and issue | `${{ inputs.dry_run }}` |
| `notify_test` | no | `"false"` | `"true"` opens and closes a test issue, then stops | `${{ inputs.notify_test }}` |
| `node_version` | no | `"22"` | Node.js for `actions/setup-node` | `"22"` |
| `publish_branch` | no | `schuettc-publish` | reserved for a later release: the sync script supports only `schuettc-publish`, so any other value fails the run in the first step | leave unset |
| `token` | no | `${{ github.token }}` | checkout, push, tags and issues on the fork | |

- `force`, `dry_run` and `notify_test` reach the script as exactly `true`, or
  `false` for anything else (including the empty string a schedule run passes).
- `build_cmd` reaches the script as-is: `""` means no build step.
- `publish_branch` is reserved. `upstream-sync.sh` pushes and tags
  `schuettc-publish` by name, so checking out any other branch would rebase it
  and force-push it over `schuettc-publish`. The action's first step fails with
  an `::error::` unless the value is `schuettc-publish`.
- The action always checks out `schuettc-publish` with full history, whatever
  ref the workflow was dispatched from. A `dry_run` dispatched from a PR branch
  therefore still exercises `schuettc-publish`, using the action at the pin on
  that branch.

The publish copy runs with `--ignore-scripts` (upstream `prepublishOnly`/`prepack`
hooks break outside the repo), so any build must be in `build_cmd`. Keep
committed lockfile changes out of the patch: if our patch adds a dependency, use
`npm install` (not `npm ci`) in the commands.

## Workflow dispatch inputs

- `dry_run`: the whole pipeline on the real runner (rebase, test, build, stamp,
  `npm pack`) with no publish, push, tag or issue. Use it after any change. It
  never exercises npm OIDC; only a real publish does.
- `notify_test`: opens and closes a test issue to prove alerts reach you.
- `force`: publish the current state even when already in sync.

## Files

| Path | Role |
|---|---|
| `action.yml` | The composite action: the `publish_branch` guard, checkout of `schuettc-publish`, setup-node, npm upgrade, then the script. |
| `upstream-sync.sh` | The sync script, run from `$GITHUB_ACTION_PATH`. Forks never carry a copy. |
| `first-publish.sh` | Builds a new fork's first-publish tarball the way CI would and prints the one publish command. |
| `tests/test.sh` | Hermetic tests for the script and `first-publish.sh` (synthetic repos, npm/gh shims). |
| `tests/action.sh` | Contract tests for `action.yml` and the caller snippet above (yq, actionlint). |
| `RUNBOOK.md` | Operating the forks. |

**Fleet data lives in tools-ops, not here.** The registry and ledger that
`muda check fork-sync` reads are `templates/fork-sync/forks.tsv` and
`templates/fork-sync/patches.tsv` in `tools-ops`; their format is unchanged.
`muda check` reads them, never rewrites them.

## Onboarding a new fork

1. `gh repo fork <upstream> --clone=false`; push our patch commits onto a
   `schuettc-publish` branch based on upstream's default branch.
2. Add the caller (above) as `.github/workflows/upstream-sync.yml`, with the
   fork's values and the newest tools-actions tag. Add `.github/dependabot.yml`:

   ```yaml
   version: 2
   updates:
     - package-ecosystem: github-actions
       directory: /
       target-branch: schuettc-publish
       schedule: { interval: weekly }
       cooldown: { default-days: 3 }
       allow:
         - dependency-name: "schuettc/tools-actions"
   ```

   Only `github-actions`: an `npm` entry would open PRs against upstream's
   dependencies. The `allow` entry restricts bumps to our own pin: without it
   Dependabot would also propose bumps to every other action `uses:`d in the
   fork's workflow files, including upstream's own disabled workflows
   (ci.yml, publish.yml, …), and each such bump becomes a fork patch that
   conflicts on rebase. Commit both as a `ci:` commit and push.
3. Repo settings: `gh api -X PATCH repos/<fork> -f default_branch=schuettc-publish -F has_issues=true`;
   enable Actions (`gh api -X PUT repos/<fork>/actions/permissions -F enabled=true -f allowed_actions=all`);
   `gh workflow disable` each upstream scheduled/tag/publish workflow.

   A fork created with `gh repo fork` holds its workflows until someone opens
   the fork's Actions tab in the browser and clicks "I understand my workflows,
   go ahead and enable them". The API call above doesn't clear it; until then
   `gh api repos/<fork>/actions/workflows` reports 0 workflows and
   `gh workflow disable` returns 404. Click it, then disable the upstream
   workflows.

   **Enable Dependabot version updates.** GitHub keeps Dependabot version
   updates off on forks by default, and there's no API for turning them on:
   once `.github/dependabot.yml` has landed, open the fork's Settings → Code
   security page and click Enable under "Dependabot version updates".
4. Dispatch `notify_test`, then `dry_run`.
5. **npm, one-time.** Trust can only attach to a package that exists, so the
   first version is published by hand. The agent runs this action's
   `first-publish.sh` from a tools-actions checkout at the tag the fork pins:

   ```sh
   git -C <tools-actions> checkout v<pinned-version>
   bash <tools-actions>/fork-sync/first-publish.sh <fork-checkout>
   ```

   It reads `pkg_name`, `pkg_dir`, `test_cmd` and `build_cmd` from the fork-sync
   step's `with:` in the fork's `upstream-sync.yml`, runs that `test_cmd` and
   `build_cmd`, stamps with the `upstream-sync.sh --stamp` beside it (so it
   matches CI exactly), strips `publishConfig.provenance` (provenance only
   works in CI), writes the tarball to `~/Desktop`, and prints one command.
   first-publish.sh reads only one-line plain or double-quoted `with:` values;
   single-quoted and block scalar forms aren't supported and may produce a bad package name. The operator only logs in
   (`npm login`, if `npm whoami` fails) and runs that `npm publish` line. Then add the trusted publisher in the npmjs.com UI
   (package → Settings; the `npm trust` CLI returned an opaque 400 for us):
   user `schuettc`, repo `<fork>`, workflow `upstream-sync.yml`,
   **environment empty**, permission **npm publish** (not staged publish). A
   package allows one publisher; replace an old one rather than adding.

   Symptoms when this is wrong: `404 PUT` = identity doesn't match (workflow
   file or environment); `403 OIDC permission denied for this action` =
   identity matches but the permission is staged-only.
6. Dispatch `force` once to prove OIDC publishing works; pin that version in
   kempt; add the fork to tools-ops' `forks.tsv`; `muda check fork-sync`.

## Migrating a fork from the carried script

Forks rendered by the old kit (muda `fork-sync`, or tools-ops
`templates/fork-sync`) carry their own `.github/scripts/upstream-sync.sh`. Move
each one to the caller with one `ci:` PR into `schuettc-publish`:

1. Replace `.github/workflows/upstream-sync.yml` with the caller above, at the
   newest tools-actions tag. Carry the fork's current `env:` values into `with:`
   (`UPSTREAM_REPO` → `upstream_repo`, …, `BUILD_CMD` → `build_cmd`) and its
   cron minute into `schedule`. The file name must stay `upstream-sync.yml`:
   npm's trusted publisher names it.
2. Delete `.github/scripts/upstream-sync.sh` and `.copier-answers.fork-sync.yml`.
3. Add `.github/dependabot.yml` (onboarding step 2): `github-actions` only,
   `target-branch: schuettc-publish`, with the `allow` entry restricting it to
   `schuettc/tools-actions`.
4. Before merging, dispatch a dry run from the PR branch:
   `gh workflow run upstream-sync.yml -R <fork> --ref <pr-branch> -f dry_run=true`.
   The run must be green, and its log must show the rebase, `test_cmd`,
   `build_cmd` and `npm pack`.
5. Merge, then enable Dependabot version updates in the fork's Settings →
   Code security (off by default on forks, no API for it; onboarding step 3),
   then wait for the fork's next scheduled run to succeed. When upstream
   has moved, `npm view <pkg> version` must match what it published.

Order across the fleet: migrate first a fork with upstream commits pending, so
its next scheduled run is a real OIDC publish through the caller (a dry run
never exercises OIDC). Only after that publish succeeds do the other forks
migrate. If trusted publishing rejects the claim, the alert files an issue
and nothing else has moved.

Until the muda Go port lands, Python `muda check fork-sync` v0.2.0 FAILs the
`sync script` and drift rows on migrated forks by design: it still looks for
the carried copy. Every other row must stay `ok`.

## Patch ledger (tools-ops `patches.tsv`)

Forks exist to be retired, so every source patch a fork carries has a stated
fate, keyed by commit subject. The ledger lives in tools-ops
(`templates/fork-sync/patches.tsv`); its subject-keyed format and statuses are
stable (any column or location change is posted on muster thread 519):

| status | meaning |
|---|---|
| `pr-open` | proposed upstream (`pr` = number); the check watches the PR |
| `not-proposed` | should go upstream eventually; `note` says what's blocking |
| `fork-forever` | specific to our setup (muster, Jev); never proposed |
| `upstreamed` / `superseded` | history: merged, or upstream shipped its own version; no longer carried |

`muda check fork-sync` **fails** on a carried patch with no row, and warns when
a watched PR merges (drop the patch) or closes, when a superseded PR is still
open, or when a row's patch is no longer carried. When you add, drop, squash or
reword a patch (subjects are the key), update the ledger in the same change.

Ledger conventions: a `ci:` commit (the caller, its Dependabot config, or a
Dependabot pin bump) is exempt from `patches.tsv`; a patch-only change (no
upstream bump) needs a `force=true` dispatch to publish.

## Retiring a fork (upstream merged our change)

1. Confirm the upstream release contains everything in our patch
   (`git log upstream/<branch>..schuettc-publish` shows only `ci:` commits).
2. Point `dotfiles/kempt.toml` back at the upstream package + version; `kempt update`.
3. `gh workflow disable upstream-sync.yml -R <fork>`; mark the row `retired` in
   tools-ops' `forks.tsv`; optionally `npm deprecate <@schuettc/name> "use <upstream-name>"`
   and archive the repo.

## The house rule

**Never hand-edit a fork's sync logic.** A fork's `upstream-sync.yml` holds
only its schedule, dispatch inputs, permissions and `with:` values. If the
sync itself is wrong, change the action here, run its tests
(`bash fork-sync/tests/test.sh` and `bash fork-sync/tests/action.sh`), release
tools-actions, and merge the Dependabot pin bumps in the forks.

## Conformance audit

```sh
muda check fork-sync \
  --forks <tools-ops>/templates/fork-sync/forks.tsv \
  --patches <tools-ops>/templates/fork-sync/patches.tsv \
  --kempt dotfiles/kempt.toml
```

Exit 0 when every fork conforms, 1 otherwise.
