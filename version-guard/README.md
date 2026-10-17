# version-guard

For tools that release when `VERSION` changes on the release branch (kempt, hail on `main`; galley, muster when `dev` is promoted to `main`). A PR into the release branch must raise `VERSION` to a newer plain `X.Y.Z`, or carry the `no-release` label. The labels are read from the API when the check runs (so the caller grants `pull-requests: read`), not from the event: a PR opened with a label otherwise fails its `opened` run.

```yaml
name: version-guard
on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, labeled, unlabeled, edited]
permissions: { contents: read, pull-requests: read }
jobs:
  version-guard:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v7.0.1
        with: { fetch-depth: 0 }
      - uses: subaud-tools/tools-actions/version-guard@v0.11.1
```
