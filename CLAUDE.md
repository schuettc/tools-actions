# Repo rules: tools-actions

This repo publishes versioned, pinned GitHub composite actions for the
`.tools` tool family and for republished npm forks. It is public. These
rules are derived from the wave-1 plan's global constraints and bind every
change here.

## Release model

- This is a **public repo**. Default branch `main` is **protected**: every
  change lands through a PR, never a direct push.
- A merge to `main` that bumps the `VERSION` file **releases**: the release
  workflow tags `v<VERSION>` and creates a GitHub release for that commit. A
  merge that doesn't bump `VERSION` releases nothing.
- Court approves PRs once CI is green.

## Pinning

- Every `uses:` in this repo's own workflows is pinned to an **exact**
  `vX.Y.Z` release tag (resolved with `gh api repos/<owner>/<repo>/releases/latest`),
  never a branch, a major-version tag, or `@main`.
- Every `runs-on:` runner is an **exact** label (e.g. `ubuntu-26.04`),
  never `-latest`.
- Actions published from this repo must document the same expectation for
  their own callers: pin `schuettc/tools-actions/<action>@vX.Y.Z`.

## Layout

- **One directory per action**, at the repo root, containing that action's
  `action.yml` and any scripts it runs.
- A repo-root directory that is **not** an action (it has no `action.yml`, is
  never a `uses:` target, and is never pinned or released) is allowed for shared
  tooling — currently `testlib/`, the ONE copy of the test-only step-variable
  guard that several actions' suites import. Such a directory must document, in
  its own README, that it is not an action.
- The `.github/workflows/` directory holds this repo's own CI/release workflows
  and any published reusable workflows; it is not an action directory.
- A composite action's scripts live **beside** its `action.yml` and are
  invoked relative to `$GITHUB_ACTION_PATH`, never assumed to be on `PATH`
  or checked out elsewhere.
- Scripts change only together with their tests in the same commit/PR —
  never a behavior change to a script without an updated test proving it.

## No project facts baked in

- Actions must not hardcode any calling project's facts: repository name,
  GitHub org, npm package name, etc. all arrive as **inputs**. An action
  must work unmodified for any caller that supplies the right inputs.

## CI and release workflows

- `ci.yml` lints all workflow YAML with a pinned `actionlint` binary
  (checksum-verified) and lints all shell scripts (`**/*.sh`) with a pinned
  `shellcheck` binary (checksum-verified). Both run on every PR and every
  push to `main`.
- `release.yml` triggers on push to `main`, resolves the release tag from
  `VERSION`, and skips cleanly if that tag's release already exists (any
  `gh` error other than "release not found" fails the job loudly rather
  than silently re-cutting or skipping). It creates the tag and GitHub
  release with `gh release create "$tag" --target "$GITHUB_SHA" --generate-notes`
  — there is no build step; the published artifact *is* the tagged source.
