#!/usr/bin/env bash
# Resolves what a release run should do and writes it to $GITHUB_OUTPUT:
#   tag, version, tool (prefixed-tag only), commit, date, create, prerelease, skip
#
# MODE=version-file (kempt, muster, galley, hail): the VERSION file is the knob.
#   push:               tag v<VERSION>; skip if that release already exists.
#   workflow_dispatch:  DISPATCH_TAG empty -> pre-release v<VERSION>-rc.<run>
#                       (the dev channel: never "latest", never collides with
#                       the v<VERSION> main publishes later);
#                       DISPATCH_TAG set   -> rebuild that existing release's
#                       assets (create=false).
# MODE=prefixed-tag (tackle): the pushed tag is <tool>/v<semver>; the tool must
#   exist at cmd/<tool>. A version with a '-' is a pre-release.
#
# Env: MODE, EVENT, DISPATCH_TAG, VERSION_FILE (default VERSION),
#      GITHUB_REF, GITHUB_REPOSITORY, GITHUB_RUN_NUMBER, GITHUB_OUTPUT.
set -euo pipefail

emit() { echo "$1=$2" >> "$GITHUB_OUTPUT"; }
die() { echo "::error::release-version: $*" >&2; exit 1; }

commit="$(git rev-parse --short HEAD)"
date="$(date -u +%Y-%m-%d)"

case "${MODE:-}" in
  version-file)
    vf="${VERSION_FILE:-VERSION}"
    [ -f "$vf" ] || die "no $vf file"
    base="$(tr -d ' \n' < "$vf")"
    [ -n "$base" ] || die "$vf is empty"
    tool=""
    if [ "${EVENT:-}" = "workflow_dispatch" ]; then
      if [ -n "${DISPATCH_TAG:-}" ]; then
        tag="$DISPATCH_TAG"; create=false; prerelease=false; skip=false
      else
        tag="v${base}-rc.${GITHUB_RUN_NUMBER:?}"; create=true; prerelease=true; skip=false
      fi
    else
      tag="v$base"; prerelease=false
      if out="$(gh release view "$tag" --repo "${GITHUB_REPOSITORY:?}" 2>&1)"; then
        echo "$tag is already released — nothing to do."
        create=false; skip=true
      elif printf '%s' "$out" | grep -q "release not found"; then
        create=true; skip=false
      else
        die "cannot tell whether $tag exists: $out"
      fi
    fi
    version="${tag#v}"
    ;;
  prefixed-tag)
    ref="${GITHUB_REF#refs/tags/}"
    case "$ref" in */v*) ;; *) die "tag '$ref' is not <tool>/v<semver>";; esac
    tool="${ref%%/*}"
    version="${ref#*/v}"
    [ -d "cmd/$tool" ] || die "tag names tool '$tool' but cmd/$tool does not exist"
    tag="$ref"; create=true; skip=false
    case "$version" in *-*) prerelease=true;; *) prerelease=false;; esac
    ;;
  *) die "mode must be version-file or prefixed-tag (got '${MODE:-}')";;
esac

emit tag "$tag"
emit version "$version"
emit tool "$tool"
emit commit "$commit"
emit date "$date"
emit create "$create"
emit prerelease "$prerelease"
emit skip "$skip"
echo "release-version: tag=$tag version=$version create=$create prerelease=$prerelease skip=$skip${tool:+ tool=$tool}"
