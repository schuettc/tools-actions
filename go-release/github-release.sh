#!/usr/bin/env bash
# Creates the GitHub release (CREATE=true) and uploads the packaged assets:
# tarballs, their .sha256 files, checksums.txt and EXTRA_ASSETS. --clobber, so
# an asset-rebuild run replaces a release's assets in place.
#
# Env: TAG, CREATE, PRERELEASE, TITLE, EXTRA_ASSETS, GITHUB_REPOSITORY,
#      GITHUB_SHA, GH_TOKEN.
set -euo pipefail
shopt -s nullglob
if [ "${CREATE:-false}" = true ]; then
  args=(release create "$TAG" --repo "$GITHUB_REPOSITORY" --target "$GITHUB_SHA" --title "${TITLE:-$TAG}" --generate-notes)
  [ "${PRERELEASE:-false}" = true ] && args+=(--prerelease)
  gh "${args[@]}"
fi
# shellcheck source=go-release/assets.sh
source "$(dirname "${BASH_SOURCE[0]}")/assets.sh"
collect_assets dist/*.tar.gz dist/*.tar.gz.sha256 dist/checksums.txt
gh release upload "$TAG" "${assets[@]}" --clobber --repo "$GITHUB_REPOSITORY"
