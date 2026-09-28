#!/usr/bin/env bash
# Imports the Developer ID Application certificate into a throwaway keychain
# and exports GO_RELEASE_KEYCHAIN and GO_RELEASE_SIGN_IDENTITY to $GITHUB_ENV.
# Fails rather than let a release ship unsigned macOS binaries: an unsigned
# binary is quarantined by Gatekeeper on download and refuses to run.
#
# Env: P12 (base64), P12_PASSWORD (may be empty), RUNNER_TEMP, GITHUB_ENV.
set -euo pipefail
[ -n "${P12:-}" ] || { echo "::error::go-release: APPLE_DEVELOPER_ID_P12 is unset — refusing to publish unsigned macOS binaries."; exit 1; }
kc="$RUNNER_TEMP/go-release.keychain-db"
kcpw="$(uuidgen 2>/dev/null || od -An -tx8 -N16 /dev/urandom | tr -d ' \n')"
cert="$RUNNER_TEMP/cert.p12"
trap 'rm -f "$cert"' EXIT
security create-keychain -p "$kcpw" "$kc"
security set-keychain-settings -lut 21600 "$kc"
security unlock-keychain -p "$kcpw" "$kc"
printf %s "$P12" | base64 --decode > "$cert"
security import "$cert" -k "$kc" -P "${P12_PASSWORD:-}" -T /usr/bin/codesign -T /usr/bin/security
# Without this, codesign blocks on a GUI keychain prompt that never comes and
# the job hangs until it times out.
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$kcpw" "$kc" >/dev/null
# shellcheck disable=SC2046  # the keychain list is intentionally word-split
security list-keychains -d user -s "$kc" $(security list-keychains -d user | tr -d '"')
identity="$(security find-identity -v -p codesigning "$kc" \
  | sed -n 's/.*"\(Developer ID Application:[^"]*\)".*/\1/p' | head -1)"
[ -n "$identity" ] || { echo "::error::go-release: no Developer ID Application identity in the imported certificate."; exit 1; }
{ echo "GO_RELEASE_KEYCHAIN=$kc"; echo "GO_RELEASE_SIGN_IDENTITY=$identity"; } >> "$GITHUB_ENV"
echo "signing as: $identity"
