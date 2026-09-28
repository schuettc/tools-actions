# release-version

Resolves what a Go tool's release run does: tag, bare semver, the commit and
date to stamp, and create / prerelease / skip. Feed its outputs to
[`go-release`](../go-release/README.md).

| Mode | Trigger | Result |
|---|---|---|
| `version-file` | push to the release branch | `v<VERSION>`; **skip** if that release exists |
| `version-file` | `workflow_dispatch`, no tag | `v<VERSION>-rc.<run>` **pre-release** (never `latest`) |
| `version-file` | `workflow_dispatch`, `tag: vX.Y.Z` | rebuild that release's assets (`create=false`) |
| `prefixed-tag` | push of `<tool>/vX.Y.Z` | one tool's release; a `-` in the version = pre-release |

```yaml
- uses: actions/checkout@v7.0.1
- id: rel
  uses: schuettc/tools-actions/release-version@v0.2.2
  with:
    mode: version-file
    dispatch-tag: ${{ inputs.tag }}
```

A `gh` error other than "release not found" fails the job rather than
re-cutting or silently skipping a release.
