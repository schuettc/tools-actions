# go-release

Releases a `.tools` Go binary: build every target with the version stamped, sign and notarize the macOS binaries, check the stamp, package, create the GitHub release, and publish to the family download contract. It runs inside your job, so your `release` environment's secrets and your job's OIDC identity are used as they are: the downloads role's trust does not change.

| Step | What it guarantees |
|---|---|
| sign | Developer ID, hardened runtime, secure timestamp. **Missing certificate = the release fails**, never ships unsigned. |
| build | `dist/<name>_<os>_<arch>/<name>`, `CGO_ENABLED=0 -trimpath -ldflags "-s -w <yours>"` |
| stamp check | each binary in `stamp-check` must print exactly `<name> <version> (<commit>, <date>)`. Only listed binaries are ever run. |
| notarize | one submission for every darwin binary; anything but `Accepted` fails |
| package | `<name>_<os>_<arch>.tar.gz`, `.tar.gz.sha256` (two-space shasum, bare asset name), `checksums.txt` (+ `extra-assets`) |
| GitHub release | created when `create`, `--prerelease` for an rc; assets uploaded with `--clobber` |
| `/dl` | `/dl/<tool>/<version>/…` immutable; `/dl/<tool>/latest` (bare semver, 60 s) and a CloudFront invalidation **only** for a created, non-prerelease release |
| cleanup | keychain, certificate and API key destroyed with `if: always()` |

```yaml
on:
  push: { branches: [main] }
  workflow_dispatch:
    inputs:
      tag: { description: "Existing tag to rebuild; empty = rc pre-release", required: false, default: "" }
permissions: { contents: write, id-token: write }
jobs:
  release:
    runs-on: macos-26
    environment: release
    steps:
      - uses: actions/checkout@v7.0.1
        with: { ref: "${{ inputs.tag }}" }
      - id: rel
        uses: schuettc/tools-actions/release-version@v0.2.1
        with: { mode: version-file, dispatch-tag: "${{ inputs.tag }}" }
      - if: steps.rel.outputs.skip != 'true'
        uses: schuettc/tools-actions/go-release@v0.2.1
        with:
          binaries: |
            kempt=./cmd/kempt
          ldflags: >-
            -X github.com/schuettc/kempt/internal/version.version={version}
            -X github.com/schuettc/kempt/internal/version.commit={commit}
            -X github.com/schuettc/kempt/internal/version.date={date}
          stamp-check: kempt
          tag: ${{ steps.rel.outputs.tag }}
          version: ${{ steps.rel.outputs.version }}
          commit: ${{ steps.rel.outputs.commit }}
          date: ${{ steps.rel.outputs.date }}
          create: ${{ steps.rel.outputs.create }}
          prerelease: ${{ steps.rel.outputs.prerelease }}
          release-title: kempt ${{ steps.rel.outputs.tag }}
          apple-developer-id-p12: ${{ secrets.APPLE_DEVELOPER_ID_P12 }}
          apple-developer-id-p12-password: ${{ secrets.APPLE_DEVELOPER_ID_P12_PASSWORD }}
          apple-notary-key: ${{ secrets.APPLE_NOTARY_KEY }}
          apple-notary-key-id: ${{ secrets.APPLE_NOTARY_KEY_ID }}
          apple-notary-issuer-id: ${{ secrets.APPLE_NOTARY_ISSUER_ID }}
          dl-tool: kempt
          dl-role-arn: ${{ secrets.KEMPT_RELEASE_ROLE_ARN }}
          dl-bucket: ${{ secrets.KEMPT_DOWNLOADS_BUCKET }}
          dl-distribution-id: E309WZW10YNEN
```

A step that must run before the build (galley's wasm client) goes before `go-release`; files the release should carry beyond the tarballs (muster's Lambda zip) are written into `dist/` first and listed in `extra-assets`.
