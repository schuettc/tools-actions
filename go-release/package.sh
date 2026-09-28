#!/usr/bin/env bash
# Tars each dist/<name>_<os>_<arch>/<name> (after signing and notarization, so
# the archives hold the signed binaries) into dist/<name>_<os>_<arch>.tar.gz,
# writes <tarball>.sha256 in the family format (two-space shasum naming the
# bare asset), and dist/checksums.txt over the tarballs and EXTRA_ASSETS.
#
# Env: EXTRA_ASSETS (newline/space-separated globs, already in dist/).
set -euo pipefail
sum() { if command -v shasum >/dev/null; then shasum -a 256 "$@"; else sha256sum "$@"; fi; }
shopt -s nullglob
dirs=(dist/*_*_*/)
[ ${#dirs[@]} -gt 0 ] || { echo "::error::go-release: nothing built in dist/"; exit 1; }
for d in "${dirs[@]}"; do
  d="${d%/}"; base="$(basename "$d")"; name="${base%_*_*}"
  tar -C "$d" -czf "dist/$base.tar.gz" "$name"
  ( cd dist && sum "$base.tar.gz" > "$base.tar.gz.sha256" )
done
extras=()
for g in ${EXTRA_ASSETS:-}; do
  for f in $g; do
    case "$f" in dist/*) extras+=("$(basename "$f")");; *) echo "::error::go-release: extra asset $f is not in dist/"; exit 1;; esac
  done
done
( cd dist && sum ./*.tar.gz ${extras[@]+"${extras[@]}"} | sed 's#  \./#  #' > checksums.txt )
echo "packaged:"; ls -1 dist/*.tar.gz dist/checksums.txt
