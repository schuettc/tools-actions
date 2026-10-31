#!/usr/bin/env bash
# Tests for go-release's scripts. Real `go build` of a tiny module; fakes for
# codesign, xcrun, security, aws and gh record their calls in $W/calls.
#
# Usage: test.sh   (needs go on PATH; exit 0 = all pass)
# check() evaluates its condition later, so conditions are single-quoted on purpose.
# shellcheck disable=SC2016,SC2034  # ...and variables they read (rc) look unused to shellcheck
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
A="$(cd "$HERE/.." && pwd)"

pass=0; failn=0
ok()   { echo "  PASS $1"; pass=$((pass+1)); }
fail() { echo "  FAIL $1"; failn=$((failn+1)); }
check() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }

command -v go >/dev/null || { echo "test.sh: needs go" >&2; exit 2; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
mkdir -p "$W/bin"
for t in codesign xcrun security aws gh ditto; do
  cat > "$W/bin/$t" <<FAKE
#!/usr/bin/env bash
echo "$t \$*" >> "$W/calls"
case "$t \$1" in
  "xcrun notarytool") [ "\$2" = submit ] && echo "  status: \${FAKE_NOTARY_STATUS:-Accepted}"; exit 0;;
  "security find-identity") echo '  1) ABC "Developer ID Application: Test (TEAM)"'; exit 0;;
  "aws s3") [ "\$2" = cp ] && [ "\$3" = - ] && cat > "$W/latest-body"; exit 0;;
esac
exit 0
FAKE
  chmod +x "$W/bin/$t"
done
cat > "$W/bin/gh" <<'FAKE'
#!/usr/bin/env bash
echo "gh $*" >> "$W/calls"
if [ "$1 $2" = "release upload" ]; then
  shift 3
  seen=()
  for arg in "$@"; do
    case "$arg" in dist/*)
      for previous in ${seen[@]+"${seen[@]}"}; do
        if [ "$arg" = "$previous" ]; then
          echo "HTTP 422: ReleaseAsset.name already exists: $arg" >&2
          exit 1
        fi
      done
      seen+=("$arg")
    esac
  done
fi
FAKE
export W
export PATH="$W/bin:$PATH"

# A tiny module whose binary prints the family version line.
mod="$W/repo"; mkdir -p "$mod/cmd/demo" "$mod/cmd/demo-deploy"
cat > "$mod/go.mod" <<'GOMOD'
module example.com/demo

go 1.22
GOMOD
for b in demo demo-deploy; do
cat > "$mod/cmd/$b/main.go" <<GOSRC
package main

import (
	"fmt"
	"os"
)

var version, commit, date string

func main() {
	if len(os.Args) > 1 && os.Args[1] == "version" {
		if "$b" == "demo-deploy" {
			fmt.Println("I AM NOT A TOOLS.APP BINARY AND MUST NOT BE RUN")
			os.Exit(9)
		}
		s := version + " (" + commit
		if date != "" {
			s += ", " + date
		}
		fmt.Println("$b " + s + ")")
	}
}
GOSRC
done


common=(VERSION=1.2.3 COMMIT=abc1234 DATE=2026-09-28
  LDFLAGS='-X main.version={version} -X main.commit={commit} -X main.date={date}'
  TARGETS="darwin/arm64 darwin/amd64 linux/amd64 linux/arm64")

echo "== build"
: > "$W/calls"
( cd "$mod" && env "${common[@]}" BINARIES=$'demo=./cmd/demo\ndemo-deploy=./cmd/demo-deploy' SIGN=true \
    GO_RELEASE_SIGN_IDENTITY="Developer ID Application: Test (TEAM)" bash "$A/build.sh" ) > "$W/log" 2>&1
check "build succeeds" '[ $? -eq 0 ] || true; [ -f "$mod/dist/demo_linux_amd64/demo" ]'
n=0; for b in demo demo-deploy; do for t in darwin_arm64 darwin_amd64 linux_amd64 linux_arm64; do [ -x "$mod/dist/${b}_${t}/$b" ] && n=$((n+1)); done; done
check "all 8 binaries built (2 bins x 4 targets)" '[ $n -eq 8 ]'
check "only darwin binaries are signed (4 codesign calls)" '[ "$(grep -c "^codesign" "$W/calls")" -eq 4 ] && ! grep "^codesign" "$W/calls" | grep -q linux'
check "codesign uses hardened runtime + timestamp" 'grep "^codesign" "$W/calls" | head -1 | grep -q -- "--options runtime --timestamp"'

: > "$W/calls"; rm -rf "$mod/dist"
( cd "$mod" && env "${common[@]}" BINARIES='demo=./cmd/demo' SIGN=false bash "$A/build.sh" ) > "$W/log" 2>&1
check "SIGN=false builds without codesign" '[ -x "$mod/dist/demo_darwin_arm64/demo" ] && ! grep -q "^codesign" "$W/calls"'

echo "== stamp-check"
rm -rf "$mod/dist"
( cd "$mod" && env "${common[@]}" BINARIES=$'demo=./cmd/demo\ndemo-deploy=./cmd/demo-deploy' SIGN=false bash "$A/build.sh" ) > "$W/log" 2>&1
( cd "$mod" && env VERSION=1.2.3 COMMIT=abc1234 DATE=2026-09-28 STAMP_CHECK=demo bash "$A/stamp-check.sh" ) > "$W/log" 2>&1
check "stamp matches: passes" '[ $? -eq 0 ] || grep -q "stamp ok" "$W/log"'
( cd "$mod" && env VERSION=1.2.3 COMMIT=abc1234 DATE=2026-09-28 STAMP_CHECK=demo bash "$A/stamp-check.sh" ) > "$W/log" 2>&1; rc=$?
check "stamp check reports ok" '[ $rc -eq 0 ] && grep -q "demo 1.2.3 (abc1234, 2026-09-28)" "$W/log"'
( cd "$mod" && env VERSION=1.2.3 COMMIT=abc1234 DATE=2026-09-29 STAMP_CHECK=demo bash "$A/stamp-check.sh" ) > "$W/log" 2>&1; rc=$?
check "wrong date fails" '[ $rc -ne 0 ]'
( cd "$mod" && env VERSION=1.2.3 COMMIT=abc1234 DATE=2026-09-28 STAMP_CHECK=demo bash "$A/stamp-check.sh" ) > "$W/log" 2>&1
check "an unlisted binary is never executed" '! grep -q "NOT A TOOLS.APP" "$W/log"'
( cd "$mod" && env VERSION=1.2.3 COMMIT=abc1234 DATE=2026-09-28 STAMP_CHECK=nothere bash "$A/stamp-check.sh" ) > "$W/log" 2>&1; rc=$?
check "a listed binary that was not built fails" '[ $rc -ne 0 ]'

echo "== notarize"
: > "$W/calls"
( cd "$mod" && env NOTARY_KEY="$(printf key | base64)" NOTARY_KEY_ID=kid NOTARY_ISSUER_ID=iss RUNNER_TEMP="$W" bash "$A/notarize.sh" ) > "$W/log" 2>&1; rc=$?
check "notarize submits and accepts" '[ $rc -eq 0 ] && grep -q "^xcrun notarytool submit" "$W/calls"'
( cd "$mod" && env FAKE_NOTARY_STATUS=Invalid NOTARY_KEY="$(printf key | base64)" NOTARY_KEY_ID=kid NOTARY_ISSUER_ID=iss RUNNER_TEMP="$W" bash "$A/notarize.sh" ) > "$W/log" 2>&1; rc=$?
check "notarize fails when not Accepted" '[ $rc -ne 0 ]'
( cd "$mod" && env NOTARY_KEY= RUNNER_TEMP="$W" bash "$A/notarize.sh" ) > "$W/log" 2>&1; rc=$?
check "notarize fails without a key" '[ $rc -ne 0 ] && grep -qi "notary" "$W/log"'

echo "== package"
printf 'zipdata' > "$mod/dist/lambda.zip"
( cd "$mod" && env EXTRA_ASSETS='dist/*.zip' bash "$A/package.sh" ) > "$W/log" 2>&1; rc=$?
check "package succeeds" '[ $rc -eq 0 ]'
n=$(find "$mod/dist" -maxdepth 1 -name '*.tar.gz' | wc -l | tr -d " ")
check "8 tarballs" '[ "$n" -eq 8 ]'
check "each tarball has a .sha256 naming the bare asset" '( for f in "$mod"/dist/*.tar.gz; do grep -qE "^[0-9a-f]{64}  $(basename "$f")$" "$f.sha256" || exit 1; done )'
check ".sha256 verifies" '( cd "$mod/dist" && shasum -a 256 -c demo_linux_amd64.tar.gz.sha256 >/dev/null )'
check "tarball holds just the binary" '[ "$(tar -tzf "$mod/dist/demo_linux_amd64.tar.gz")" = "demo" ]'
check "checksums.txt covers tarballs and extra assets" '[ "$(wc -l < "$mod/dist/checksums.txt" | tr -d " ")" -eq 9 ] && grep -q "  lambda.zip$" "$mod/dist/checksums.txt"'

echo "== github-release"
: > "$W/calls"
( cd "$mod" && env TAG=v1.2.3 CREATE=true PRERELEASE=false TITLE="demo v1.2.3" EXTRA_ASSETS='dist/*.zip' GITHUB_REPOSITORY=o/r GITHUB_SHA=deadbeef bash "$A/github-release.sh" ) > "$W/log" 2>&1; rc=$?
check "creates the release at the commit, with notes" '[ $rc -eq 0 ] && grep -q "^gh release create v1.2.3 --repo o/r --target deadbeef --title demo v1.2.3 --generate-notes$" "$W/calls"'
check "uploads tarballs, .sha256, checksums.txt and extras with --clobber" 'grep "^gh release upload v1.2.3" "$W/calls" | grep -q "checksums.txt" && grep "^gh release upload" "$W/calls" | grep -q "lambda.zip" && grep "^gh release upload" "$W/calls" | grep -q "demo_linux_amd64.tar.gz.sha256" && grep "^gh release upload" "$W/calls" | grep -q -- "--clobber"'
: > "$W/calls"
( cd "$mod" && env TAG=v1.2.3-rc.4 CREATE=true PRERELEASE=true GITHUB_REPOSITORY=o/r GITHUB_SHA=deadbeef bash "$A/github-release.sh" ) > "$W/log" 2>&1
check "an rc is created as a pre-release" 'grep "^gh release create" "$W/calls" | grep -q -- "--prerelease"'
: > "$W/calls"
( cd "$mod" && env TAG=v1.1.0 CREATE=false GITHUB_REPOSITORY=o/r GITHUB_SHA=deadbeef bash "$A/github-release.sh" ) > "$W/log" 2>&1
check "an asset rebuild creates nothing, only uploads" '! grep -q "^gh release create" "$W/calls" && grep -q "^gh release upload v1.1.0" "$W/calls"'

echo "== publish-dl"
: > "$W/calls"
( cd "$mod" && env DL_TOOL=demo BUCKET=bkt VERSION=1.2.3 PROMOTE=true DISTRIBUTION_ID=EDIST EXTRA_ASSETS='dist/*.zip' bash "$A/publish-dl.sh" ) > "$W/log" 2>&1; rc=$?
check "publish succeeds" '[ $rc -eq 0 ]'
check "18 versioned uploads (8 tarballs + 1 extra asset, each with a .sha256)" '[ "$(grep -c "s3://bkt/dl/demo/1.2.3/" "$W/calls")" -eq 18 ]'
check "versioned paths are immutable" '! grep "s3://bkt/dl/demo/1.2.3/" "$W/calls" | grep -vq "max-age=31536000,immutable"'
check ".sha256 is text/plain" '! grep "\.sha256 " "$W/calls" | grep -vq "content-type text/plain"'
check "extra assets are published beside the tarballs" 'grep -q "dist/lambda.zip s3://bkt/dl/demo/1.2.3/lambda.zip " "$W/calls" && grep -q "dist/lambda.zip.sha256 s3://bkt/dl/demo/1.2.3/lambda.zip.sha256 " "$W/calls"'
check "an extra asset's .sha256 names the bare asset and verifies" 'grep -qE "^[0-9a-f]{64}  lambda.zip$" "$mod/dist/lambda.zip.sha256" && ( cd "$mod/dist" && shasum -a 256 -c lambda.zip.sha256 >/dev/null )'
check "latest written with the bare semver" 'grep -q "s3://bkt/dl/demo/latest" "$W/calls" && [ "$(cat "$W/latest-body")" = "1.2.3" ]'
check "latest is short-TTL" 'grep "dl/demo/latest" "$W/calls" | grep -q "max-age=60,must-revalidate"'
check "latest invalidated" 'grep -q "cloudfront create-invalidation --distribution-id EDIST --paths /dl/demo/latest" "$W/calls"'
: > "$W/calls"
( cd "$mod" && env DL_TOOL=demo BUCKET=bkt VERSION=1.2.3-rc.4 PROMOTE=false DISTRIBUTION_ID=EDIST bash "$A/publish-dl.sh" ) > "$W/log" 2>&1
check "PROMOTE=false never touches latest" '! grep -q "dl/demo/latest" "$W/calls" && ! grep -q "create-invalidation" "$W/calls"'
: > "$W/calls"
( cd "$mod" && env DL_TOOL=demo BINARY_PREFIXES="demo" BUCKET=bkt VERSION=1.2.3 PROMOTE=false DISTRIBUTION_ID=EDIST bash "$A/publish-dl.sh" ) > "$W/log" 2>&1
check "tarballs of every packaged binary go under the tool's path" 'grep -q "dl/demo/1.2.3/demo-deploy_linux_amd64.tar.gz" "$W/calls"'

echo "== overlapping setup TAR extra"
printf 'setup' > "$W/setup.toml"
tar -C "$W" -czf "$mod/dist/demo_setup.tar.gz" setup.toml
( cd "$mod" && env EXTRA_ASSETS='dist/demo_setup.tar.gz dist/*.zip' bash "$A/package.sh" ) > "$W/log" 2>&1; rc=$?
check "overlapping TAR has exactly one aggregate checksum" '[ $rc -eq 0 ] && [ "$(grep -c "  demo_setup.tar.gz$" "$mod/dist/checksums.txt")" -eq 1 ] && [ "$(wc -l < "$mod/dist/checksums.txt")" -eq 10 ]'
: > "$W/calls"
( cd "$mod" && env TAG=v1.2.3 CREATE=false EXTRA_ASSETS='dist/demo_setup.tar.gz dist/*.zip' GITHUB_REPOSITORY=o/r bash "$A/github-release.sh" ) > "$W/log" 2>&1; rc=$?
cat "$W/log"
check "overlapping TAR uploads without duplicate argument HTTP422" '[ $rc -eq 0 ] && [ "$(grep "^gh release upload" "$W/calls" | tr " " "\n" | grep -cx "dist/demo_setup.tar.gz")" -eq 1 ]'
: > "$W/calls"
( cd "$mod" && env DL_TOOL=demo BUCKET=bkt VERSION=1.2.3 PROMOTE=false EXTRA_ASSETS='dist/demo_setup.tar.gz dist/*.zip' bash "$A/publish-dl.sh" ) > "$W/log" 2>&1; rc=$?
check "overlapping TAR and sidecar each copied once" '[ $rc -eq 0 ] && [ "$(grep -c "^aws s3 cp dist/demo_setup.tar.gz " "$W/calls")" -eq 1 ] && [ "$(grep -c "^aws s3 cp dist/demo_setup.tar.gz.sha256 " "$W/calls")" -eq 1 ] && [ "$(grep -c "s3://bkt/dl/demo/1.2.3/" "$W/calls")" -eq 20 ]'
check "setup sidecar verifies and rebuild does not promote" '( cd "$mod/dist" && shasum -a 256 -c demo_setup.tar.gz.sha256 >/dev/null ) && ! grep -q "latest" "$W/calls"'

echo "== sign-setup and cleanup"
( env P12= GITHUB_ENV="$W/env" RUNNER_TEMP="$W" bash "$A/sign-setup.sh" ) > "$W/log" 2>&1; rc=$?
check "sign-setup fails without the certificate" '[ $rc -ne 0 ] && grep -q "APPLE_DEVELOPER_ID_P12" "$W/log"'
: > "$W/env"; : > "$W/calls"
( env P12="$(printf p12 | base64)" P12_PASSWORD= GITHUB_ENV="$W/env" RUNNER_TEMP="$W" bash "$A/sign-setup.sh" ) > "$W/log" 2>&1; rc=$?
check "sign-setup exports the identity and keychain" '[ $rc -eq 0 ] && grep -q "^GO_RELEASE_SIGN_IDENTITY=Developer ID Application: Test (TEAM)$" "$W/env" && grep -q "^GO_RELEASE_KEYCHAIN=" "$W/env"'
check "sign-setup removes the decoded certificate" '[ ! -e "$W/cert.p12" ]'
: > "$W/calls"; touch "$W/notary.p8" "$W/cert.p12" "$W/notarize.zip"
( env GO_RELEASE_KEYCHAIN="$W/k.keychain-db" RUNNER_TEMP="$W" bash "$A/cleanup.sh" ) > "$W/log" 2>&1; rc=$?
check "cleanup deletes key files and the keychain" '[ $rc -eq 0 ] && [ ! -e "$W/notary.p8" ] && [ ! -e "$W/cert.p12" ] && grep -q "^security delete-keychain" "$W/calls"'
( env RUNNER_TEMP="$W" bash "$A/cleanup.sh" ) > "$W/log" 2>&1; rc=$?
check "cleanup succeeds when nothing was set up" '[ $rc -eq 0 ]'

echo; echo "passed $pass, failed $failn"
[ "$failn" -eq 0 ]
