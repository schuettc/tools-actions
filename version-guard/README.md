# version-guard

For tools that release when `VERSION` changes on the release branch (kempt, hail on `main`; galley, muster when `dev` is promoted to `main`). A PR into the release branch must raise `VERSION` to a newer plain `X.Y.Z`, or carry the `no-release` label.

```yaml
name: version-guard
on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, labeled, unlabeled, edited]
permissions: { contents: read }
jobs:
  version-guard:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v7.0.1
        with: { fetch-depth: 0 }
      - uses: schuettc/tools-actions/version-guard@v0.4.0
```
