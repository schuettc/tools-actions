# tools-actions

Versioned, pinned GitHub composite actions for the `.tools` tool family and
for republished npm forks.

## What's published

Every directory at the repo root that contains an `action.yml` is a
standalone composite action, published as part of a single repo-wide
release:

- [`fork-sync`](fork-sync/README.md): keeps a republished npm fork rebased on
  upstream and publishes it to npm with trusted publishing.
- [`tested-tree`](tested-tree/README.md): tells a deploy workflow whether this
  exact tree already passed the repo's PR CI, so it can skip mirrored checks.

Each action's README describes its inputs, outputs, and usage.

## Pinning

Callers must pin the action to an exact release tag, never a branch or
`@main`:

```yaml
- uses: schuettc/tools-actions/<action>@vX.Y.Z
```

`<action>` is the directory name, e.g. `schuettc/tools-actions/fork-sync@v0.1.0`.
Floating references (`@main`, `@v1`, no tag at all) are not supported and
must not be used.

## Releases

`main` is protected; every change lands through a PR. The `VERSION` file at
the repo root is the release knob: a merge to `main` that bumps `VERSION`
triggers the release workflow, which tags `v<VERSION>` and creates a GitHub
release for that commit. A merge that doesn't bump `VERSION` releases
nothing. All actions in the repo share one version; there is no per-action
versioning.

## Dependabot

This repo's own `.github/workflows/*.yml` pin `uses:` references to exact
versions. Dependabot's `github-actions` ecosystem entry (see
`.github/dependabot.yml`) opens PRs to bump those pins on a weekly cadence
with a cooldown, so this repo's own CI/release workflows stay current the
same way any caller's fork of these actions would.

Consumers of these actions get updates the same way: point Dependabot's
`github-actions` ecosystem at the caller repo, and it will propose bumping
`schuettc/tools-actions/<action>@vX.Y.Z` pins as new releases are cut here.

## License

MIT. See `LICENSE`.
