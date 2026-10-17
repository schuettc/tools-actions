#!/usr/bin/env bash
# Merging to the release branch with an unbumped VERSION silently releases
# nothing (release-version sees the tag already released and skips). A PR into
# the release branch must therefore raise VERSION to a newer plain semver, or
# carry the `no-release` label (docs/CI-only changes).
#
# Env: BASE (the PR's base sha), PR_NUMBER, REPO (owner/name), GH_TOKEN.
#
# Labels are read from the API at run time, not from the event payload: a PR
# opened with a label (`gh pr create --label`) starts an `opened` run whose
# payload has no labels yet, and that stale failure otherwise sticks to the PR.
set -euo pipefail
labels="$(gh api "repos/${REPO:?}/issues/${PR_NUMBER:?}/labels" --jq '.[].name')" || {
  echo "::error::cannot read the labels of PR #$PR_NUMBER (needs pull-requests: read)"; exit 1; }
case " $(echo $labels) " in
  *" no-release "*) echo "no-release label present: skipping the VERSION check."; exit 0 ;;
esac
semver='^[0-9]+\.[0-9]+\.[0-9]+$'
new="$(tr -d ' \n' < VERSION)"
old="$(git show "${BASE:?}:VERSION" 2>/dev/null | tr -d ' \n')" || { echo "::error::cannot read VERSION at base $BASE"; exit 1; }
if [ "$new" = "$old" ]; then
  echo "::error file=VERSION::VERSION is unchanged ($old). Bump it (this PR releases on merge) or add the 'no-release' label."; exit 1
fi
[[ "$new" =~ $semver ]] || { echo "::error file=VERSION::VERSION '$new' is not plain semver X.Y.Z (pre-releases come from workflow_dispatch, not VERSION)."; exit 1; }
if [[ "$old" =~ $semver ]]; then
  IFS=. read -r a b c <<< "$old"; IFS=. read -r x y z <<< "$new"
  if (( x < a || (x == a && (y < b || (y == b && z <= c))) )); then
    echo "::error file=VERSION::VERSION $new is not newer than $old."; exit 1
  fi
fi
echo "VERSION bumped: $old -> $new"
