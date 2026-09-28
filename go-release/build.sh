#!/usr/bin/env bash
# Builds each binary for each target into dist/<name>_<os>_<arch>/<name>,
# stamping the version via -ldflags, and signs the darwin ones (hardened
# runtime + secure timestamp, which notarization requires).
#
# Env: BINARIES (lines "name=./cmd/pkg"), LDFLAGS (with {version} {commit}
#      {date} placeholders), VERSION, COMMIT, DATE, TARGETS, BUILD_TAGS,
#      SIGN (true|false), GO_RELEASE_SIGN_IDENTITY (when SIGN=true).
set -euo pipefail
[ -n "${BINARIES:-}" ] || { echo "::error::go-release: binaries is empty"; exit 1; }
# shellcheck disable=SC2153  # LDFLAGS arrives from the environment
ldflags="${LDFLAGS//\{version\}/$VERSION}"
ldflags="${ldflags//\{commit\}/$COMMIT}"
ldflags="${ldflags//\{date\}/$DATE}"
ldflags="-s -w $ldflags"
tags=()
[ -n "${BUILD_TAGS:-}" ] && tags=(-tags "$BUILD_TAGS")
if [ "${SIGN:-true}" = true ] && [ -z "${GO_RELEASE_SIGN_IDENTITY:-}" ]; then
  echo "::error::go-release: sign is true but no signing identity was set up"; exit 1
fi
while IFS= read -r line; do
  line="${line%%#*}"; line="$(printf '%s' "$line" | tr -d '[:space:]')"
  [ -n "$line" ] || continue
  name="${line%%=*}"; pkg="${line#*=}"
  [ -n "$name" ] && [ -n "$pkg" ] && [ "$name" != "$line" ] || { echo "::error::go-release: bad binaries line '$line' (want name=./cmd/pkg)"; exit 1; }
  for target in $TARGETS; do
    goos="${target%/*}"; goarch="${target#*/}"
    out="dist/${name}_${goos}_${goarch}/${name}"
    CGO_ENABLED=0 GOOS="$goos" GOARCH="$goarch" \
      go build -trimpath ${tags[@]+"${tags[@]}"} -ldflags "$ldflags" -o "$out" "$pkg"
    if [ "${SIGN:-true}" = true ] && [ "$goos" = darwin ]; then
      codesign --sign "$GO_RELEASE_SIGN_IDENTITY" --options runtime --timestamp --force "$out"
    fi
    echo "built $out"
  done
done <<< "$BINARIES"
