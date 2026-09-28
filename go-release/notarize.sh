#!/usr/bin/env bash
# Notarizes every signed darwin binary in dist/ in ONE submission (Apple takes
# minutes per submission). Bare executables cannot be stapled, so Gatekeeper
# verifies them online.
#
# Env: NOTARY_KEY (base64 .p8), NOTARY_KEY_ID, NOTARY_ISSUER_ID, RUNNER_TEMP.
set -euo pipefail
[ -n "${NOTARY_KEY:-}" ] || { echo "::error::go-release: APPLE_NOTARY_KEY is unset — cannot notarize."; exit 1; }
key="$RUNNER_TEMP/notary.p8"; zip="$RUNNER_TEMP/notarize.zip"; log="$RUNNER_TEMP/notary.log"
printf %s "$NOTARY_KEY" | base64 --decode > "$key"
stage="$RUNNER_TEMP/notarize"; rm -rf "$stage"; mkdir -p "$stage"
for d in dist/*_darwin_*/; do [ -d "$d" ] && cp -R "$d" "$stage/"; done
ditto -c -k --keepParent "$stage" "$zip"
xcrun notarytool submit "$zip" --key "$key" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER_ID" \
  --wait --timeout 30m | tee "$log"
if ! grep -q "status: Accepted" "$log"; then
  echo "::error::go-release: notarization did not return Accepted"
  xcrun notarytool log "$(awk '/id:/{print $2; exit}' "$log")" \
    --key "$key" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER_ID" || true
  exit 1
fi
