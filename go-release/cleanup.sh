#!/usr/bin/env bash
# Destroys the signing material. Runs with if: always(), because a failed
# build must not leave a usable Developer ID certificate or App Store Connect
# key behind on the runner.
set -uo pipefail
rm -rf "$RUNNER_TEMP/notary.p8" "$RUNNER_TEMP/cert.p12" "$RUNNER_TEMP/notarize.zip" "$RUNNER_TEMP/notarize"
if [ -n "${GO_RELEASE_KEYCHAIN:-}" ]; then
  security delete-keychain "$GO_RELEASE_KEYCHAIN" || true
fi
exit 0
