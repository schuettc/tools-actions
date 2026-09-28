# fork-sync runbook — operating the forks locally

Companion to [`README.md`](README.md) (the model, the caller, onboarding and
retirement). This is the **hands-on** side: what to do at your machine when a
fork needs attention, and the traps we hit doing it for the first time
(2026-09-22/23). Written task-first so it can become a skill: each `##` is a
situation, with the trigger, the steps, and how you know it worked.

---

## Quick reference

| Fork | Local checkout (remotes) | Upstream | Package dir | Tag prefix | Cron (UTC) |
|---|---|---|---|---|---|
| `schuettc/pi-claude-bridge` | `~/GitHub/worktrees/pi-claude-bridge-current` (**`origin` = upstream elidickinson**, `schuettc` = fork, `karrq`) | `elidickinson/pi-claude-bridge@main` | `.` | `v` | 09:17 |
| `schuettc/pi-packages` → `@schuettc/pi-auto-review` | `~/GitHub/schuettc/pi-packages` (`origin` = fork, `upstream`) | `erichll/pi-packages@main` | `packages/pi-auto-review` | `pi-auto-review-v` | 09:23 |
| `schuettc/pi-schedule` | `tools-workspace/pi-schedule` (`origin` = fork, `upstream`) | `pungggi/pi-schedule@master` | `.` | `v` | 09:29 |
| `schuettc/pi-usage` | none kept; clone fresh (see below) | `Sreetej510/pi-extensions@master` | `extensions/pi-usage` | `pi-usage-v` | 09:41 |

> **Remote-name trap:** in the bridge checkouts `origin` is the *upstream*
> project. Push the fork with `git push schuettc …`, never `origin`. Check
> `git remote -v` before any push.

Fresh clone of any fork, standard remote layout:

```bash
git clone git@github.com:schuettc/<fork>.git ~/GitHub/worktrees/<fork>-work
cd ~/GitHub/worktrees/<fork>-work
git remote add upstream https://github.com/<upstream-owner>/<upstream-repo>.git
git fetch upstream && git checkout schuettc-publish
```

Health of everything, one command (fleet data stays in tools-ops, D11):

```bash
muda check fork-sync \
  --forks <tools-ops>/templates/fork-sync/forks.tsv \
  --patches <tools-ops>/templates/fork-sync/patches.tsv \
  --kempt dotfiles/kempt.toml        # exit 0 = all forks conform
```

---

## Situation: an "upstream-sync failed" / "manual rebase needed" email

**Trigger:** GitHub issue on a fork (you're @mentioned), red run.

1. Read the issue: it names the **phase** (`rebase`, `test`, `build`,
   `publish`, `push`) and links the run.
   - `rebase` → a genuine conflict with our source patches → **catch-up rebase** (below).
   - `test` → upstream changed something our patch depends on; reproduce with
     the fork's `test_cmd` locally after rebasing (below).
   - `publish` → almost always npm config → **npm errors** (below).
2. Read the run log without the noise:

   ```bash
   gh run view <run-id> -R schuettc/<fork> --log | sed 's/\x1b\[[0-9;]*m//g' \
     | grep -E 'ahead of|# (pass|fail) [0-9]+|Tests +[0-9]+|publishing new version|Published @|npm error|##\[error\]'
   ```
3. When fixed and republished, close the issue with a one-line cause. Retries
   comment on the same issue; they don't open new ones.

**Never** hand-edit a fork's sync logic: a fork carries only the pinned caller
(`.github/workflows/upstream-sync.yml`). If the sync is wrong, change the action
in tools-actions (see "Changing the standard").

---

## Situation: catch-up rebase (the cron hit a real conflict)

This is the one that needs judgement. Done for bridge 0.7→0.8 (16 commits
against 19 upstream) and pi-usage (22 commits → 2).

1. **Work in a throwaway worktree**, never over someone's in-progress branch.
   `git stash` is blocked by policy here; use a worktree instead.

   ```bash
   git -C <checkout> fetch <fork-remote> && git -C <checkout> fetch upstream
   git -C <checkout> worktree add --detach ~/GitHub/worktrees/<fork>-catchup <fork-remote>/schuettc-publish
   ```
2. **Look before resolving.** List our patches and upstream's new commits:

   ```bash
   git log --oneline upstream/<branch>..HEAD          # ours
   git log --oneline $(git merge-base HEAD upstream/<branch>)..upstream/<branch>   # theirs
   git show --stat <each-of-ours>                      # footprint per patch
   ```
3. **Triage each of our patches** against upstream:
   - **Upstream adopted it** (often our own PR, landed under their name) →
     **drop ours, use theirs.** The stack shrinks; this is how forks retire.
     Match by behaviour, not commit message; read both diffs.
   - **Genuinely ours** → keep, re-apply.
   - **Packaging churn** (version bumps, `chore(release)`, scoped renames,
     CHANGELOG entries, lockfile noise, old CI) → drop. The standard stamps
     metadata at publish time; none of it belongs on the branch.
4. **"Upstream adopted it" is a claim; test it.** Before dropping a patch as
   superseded, run **that patch's own tests** (from the old stack or its PR
   branch) against the new base. If they fail, upstream did not cover it.
   We learned this the hard way: bridge's side-request patch (#97) was
   dropped in the 0.8.0 catch-up because upstream's isolated path *looked*
   equivalent; its integration test left with it, every remaining suite
   passed, and extensions' own model calls were broken until the test was
   re-run a day later (`No API provider registered for api: claude-bridge`).
5. **Before dropping anything, prove it isn't load-bearing.** Grep the
   consumers. Example: bridge's `AGENT_SESSION_ID` stamping looked redundant
   with `MUSTER_HOOK_DISABLE`, but muster reads it on its **MCP caller-identity**
   path too (`muster/internal/mcpserver/caller_identity.go`), not just hooks, so
   we kept it. A test in the old stack asserting the behaviour is a strong hint.
6. **Undo/redo stacks: apply the net diff once, don't replay history.** If
   later commits revert earlier ones (bridge's usage meter: disable → re-enable),
   replaying makes you resolve the same hunk several times. Instead:

   ```bash
   git checkout -B schuettc-publish-new upstream/<branch>
   git checkout <old-tip> -- <new files that are purely ours>
   git diff <first-kept>~1 <last-kept> -- <shared file> > /tmp/net.diff
   git apply --3way /tmp/net.diff                      # resolve the few real conflicts
   ```
   Small, independent patches can just be cherry-picked (`git cherry-pick -x`).
7. **Resolve conflicts toward upstream's structure.** Take their refactor,
   re-insert our hooks. Watch for symbols upstream removed that our patch still
   references (`tsc --noEmit` finds them) and name collisions with new upstream
   locals (we renamed `childEnv()` → `stampedChildEnv()` for this).
8. **Put package metadata back to upstream's.** `name`, `version`,
   `repository`, README install lines stay upstream's; only genuine changes
   (new deps, test script) remain in `package.json`. Lockfile: prefer
   upstream's; if our patch adds deps, use `npm install` (not `npm ci`) in
   `test_cmd`.
9. **Test locally** (next section), then commit with a message that lists what
   was kept and what was dropped as upstream-adopted.
10. **Ship:**

   ```bash
   git tag archive/schuettc-publish-pre-<date> <fork-remote>/schuettc-publish   # when rewriting history
   git push <fork-remote> archive/schuettc-publish-pre-<date>
   git push --force-with-lease=schuettc-publish:$(git rev-parse <fork-remote>/schuettc-publish) \
     <fork-remote> HEAD:schuettc-publish
   gh workflow run upstream-sync.yml -R schuettc/<fork> --ref schuettc-publish -f dry_run=true
   gh workflow run upstream-sync.yml -R schuettc/<fork> --ref schuettc-publish -f force=true
   ```
   The branch is now rebased on upstream, so a plain run sees `ahead=0` and
   skips. Publishing the catch-up needs `force=true` (a patch-only change, with
   no upstream bump, is exactly this case).
11. Pin the new version in kempt (below), and update tools-ops' `patches.tsv`.

---

## Situation: testing a fork locally

- Run the fork's own `test_cmd`, the exact gate CI uses (the fork-sync step's
  `with:` in the fork's `upstream-sync.yml`). E.g. bridge: `npm ci && npm run typecheck && npm run test:unit`.
- Unit tests are the gate. Some forks have extra **integration** suites that
  CI can't run:
  - **pi-claude-bridge** `npm test` = unit + `int-smoke` + `int-multi-turn` +
    `int-cache` + `tests/int-*.mjs`, against **live Claude** (your login).
    Run the claude-bridge suites directly when `npm test` short-circuits:
    `./tests/int-multi-turn.sh`, `./tests/int-cache.sh`,
    `node --import tsx --test $(ls tests/int-*.mjs | grep -vE 'int-image-session|int-session-resume')`.
  - Bridge suites needing `CLAUDE_BRIDGE_TESTING_ALT_PROVIDER` (the AskClaude
    ones: `int-smoke`'s AskClaude case, `int-image-session`,
    `int-session-resume`'s turns 3–4) need a **non-claude-bridge** provider
    with a key. Upstream uses MiniMax; **we have no MiniMax key**. AskClaude is
    opt-in and disabled in our config, so skipping these is acceptable when our
    diff doesn't touch AskClaude (verify: `git diff upstream/main -- src/askclaude-ui.ts` is empty).
  - `.env.test` holds those settings; agent policy blocks reading or copying
    `.env*` files. Only the user's own shell (or `npm test`, which sources it)
    can use it.
- **Publish dry run**, for the full CI pipeline on the real runner:
  `gh workflow run upstream-sync.yml -R schuettc/<fork> --ref schuettc-publish -f dry_run=true`.

---

## Situation: npm account steps (need Court's npm login)

The agent is not logged in to npm; these are hand-offs.

**First publish of a new fork** (trust can only attach to an existing package).
The agent builds the tarball; the operator logs in and publishes:

```bash
git -C <tools-actions> checkout v<pinned-version>                # agent: the tag the fork pins
bash <tools-actions>/fork-sync/first-publish.sh <fork-checkout>  # agent: prints the command below
npm login                                               # operator, only if `npm whoami` fails
npm publish ~/Desktop/<pkg>-<ver>-schuettc.1.tgz --access public --ignore-scripts --tag latest
```
- `first-publish.sh` refuses when the package already exists on npm (later
  versions go through the workflow's `force` dispatch), when the checkout isn't
  exactly `origin/schuettc-publish`, or when the working tree has changes.
- `--tag latest` is required: npm refuses prerelease-shaped versions
  (`x.y.z-schuettc.N`) without an explicit tag.

**Trusted publisher:** set it in the **npmjs.com UI** (package → Settings →
Trusted Publisher → GitHub Actions). `npm trust github` returned an opaque
`400` for us twice.

| Field | Value |
|---|---|
| Organization or user | `schuettc` |
| Repository | `<fork>` |
| Workflow filename | `upstream-sync.yml` |
| Environment | **empty** |
| Permission | **npm publish** (not "staged publish") |

One publisher per package; **replace** an old one, don't add a second.

**Reading publish failures in CI:**

| Error | Meaning | Fix |
|---|---|---|
| `404 Not Found - PUT …` | OIDC identity doesn't match the trusted publisher (wrong workflow file or environment) | fix the publisher fields |
| `403 OIDC permission denied for this action` | identity matches, but permission is staged-only | set permission to **npm publish** |
| `E400` from `npm trust` | CLI/registry quirk | use the web UI |

---

## Situation: pinning and applying a release

```bash
# 1. wait until the exact version resolves (the registry lags 1–5 min after a publish)
npm view @schuettc/<pkg>@<version> version
# 2. bump both occurrences in dotfiles/kempt.toml (list form + settings merge line)
grep -n '@schuettc/<pkg>@' dotfiles/kempt.toml
#    an alias-pinned fork (see README "Alias pins") reads
#    npm:<upstream-name>@npm:@schuettc/<pkg>@<version>; bump the version at the end
# 3. commit dotfiles, then apply
kempt update            # takes effect on the next pi launch, not the running session
```
- **Rollback:** revert the pin in kempt.toml to the previous version, then
  `kempt update`. Old versions stay on npm.
- A fork's publish never changes what runs on its own; only the kempt pin does.

---

## Situation: changing the standard itself

The model: change the action, run its tests, release, merge the forks'
Dependabot bumps. Nothing is copied into a fork.

1. Edit `fork-sync/upstream-sync.sh` and/or `fork-sync/action.yml` in
   tools-actions, on a PR branch. A script change lands with its test in the
   same PR.
2. `bash fork-sync/tests/test.sh` must pass. On macOS it re-runs itself in a
   `node:22-bookworm` container (needs Docker) because CI runs **bash 5** and
   macOS ships **bash 3.2**; 3.2 hid a real bug (ERR trap + subshell → two issues
   per failure).
3. `bash fork-sync/tests/action.sh` must pass (needs `yq` v4 and `actionlint`):
   it checks `action.yml`'s inputs and env mapping and lints the README caller
   snippet.
4. For any new behaviour, add a test **and** mutation-check it: break the
   script on purpose (`SCRIPT=<mutated copy> bash fork-sync/tests/test.sh`)
   and confirm a test fails.
5. Release: bump `VERSION` in the same PR and update the README caller
   snippet's pin to match (`action.sh` fails until they agree). CI green, Court
   approves, the merge tags `v<VERSION>`.
6. Roll out: Dependabot opens a `github-actions` PR against `schuettc-publish`
   in each fork in tools-ops' `forks.tsv` (weekly, after a 3-day cooldown). Merge
   each one; its `ci:` commit is exempt from `patches.tsv`. For an urgent fix,
   bump the `@vX.Y.Z` pin by hand in the same way.
7. `muda check fork-sync` shows green, then dispatch `dry_run` on each fork.

---

## Environment traps (the short list)

- **gh picks the wrong repo:** with a remote named `upstream`, `gh` defaults to
  it. Always pass `-R schuettc/<fork>`. (This is why issue alerts silently
  failed before the standard.)
- **Registry lag:** `npm view` returns 404 or the old `latest` for minutes after
  a successful publish. Trust the run log's `Published @…` line and the pushed
  git tag, then poll the exact version.
- **Upstream workflows ride along** on a fork (release, publish, compat
  crons). Disable every one except `upstream-sync` (`gh workflow disable
  <file> -R schuettc/<fork>`); `muda check fork-sync` flags stragglers.
- **Cron only fires on the default branch:** the fork's default branch must be
  `schuettc-publish` (pi-usage's old sync never ran because it lived elsewhere).
- **`--ignore-scripts` on publish:** upstream `prepublishOnly`/`prepack` hooks
  (monorepo-relative builds, "publish via CI only" guards) break in the isolated
  copy. Anything that must happen before packing goes in `build_cmd`.
- **Naming:** `@schuettc/<name>` only for forks of others' packages; packages
  we own publish unscoped.
- **Coordinate first:** check `muster` for a live session on the same repo and
  message it before taking over a branch.
