#!/usr/bin/env bash
# Runs the family Go gate locally, at exactly the tools-actions version this
# repo's CI pins, so `just verify` (and the pre-push hook) and CI can never
# disagree. Every repo's justfile calls this through one standard recipe.
#
# 1. Read the `schuettc/tools-actions/go-ci@vX.Y.Z` pin from
#    .github/workflows/ci.yml (the one pin Dependabot bumps).
# 2. Fetch that version's ci.sh, family golangci.yml and golangci-lint.lock
#    into ~/.cache/tools-actions/vX.Y.Z/ (kept; later runs work offline).
# 3. Use golangci-lint from PATH if it is the locked version, else install the
#    locked version into the cache (checksum-verified) and use that.
# 4. Run Go at the version CI's actions/setup-go installs from go.mod (see
#    below), not whatever Go is on PATH.
# 5. Run the gate with the same settings CI uses.
#
# Env: TOOLS_ACTIONS_BASE (default https://raw.githubusercontent.com/schuettc/tools-actions),
#      GOLANGCI_DL_BASE (default https://github.com/golangci/golangci-lint/releases/download),
#      GO_DL_JSON (default https://go.dev/dl/?mode=json&include=all), GOTOOLCHAIN (kept if set),
#      XDG_CACHE_HOME, and ci.sh's own RACE / TARGETS / PACKAGES / SKIP_LINT.
set -euo pipefail
die() { echo "go-ci local: $*" >&2; exit 2; }

ci=.github/workflows/ci.yml
[ -f "$ci" ] || die "no $ci here: run from the repo root (the gate runs at the version $ci pins)"
ver="$(grep -oE 'schuettc/tools-actions/go-ci@v[0-9]+\.[0-9]+\.[0-9]+' "$ci" | head -1 | cut -d@ -f2 || true)"
[ -n "$ver" ] || die "$ci has no schuettc/tools-actions/go-ci@vX.Y.Z pin"

base="${TOOLS_ACTIONS_BASE:-https://raw.githubusercontent.com/schuettc/tools-actions}"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/tools-actions"
dir="$cache/$ver/go-ci"
if [ ! -f "$dir/.complete" ]; then
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  for f in ci.sh golangci.yml golangci-lint.lock; do
    curl -fsSL "$base/$ver/go-ci/$f" -o "$tmp/$f" || die "could not fetch tools-actions $ver go-ci/$f from $base"
  done
  mkdir -p "$dir" && cp "$tmp"/* "$dir/" && touch "$dir/.complete"
fi

want="$(awk '$1=="version"{print $2}' "$dir/golangci-lint.lock")"
have=""; command -v golangci-lint >/dev/null 2>&1 && have="$(golangci-lint --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
if [ -n "$want" ] && [ "$have" != "$want" ]; then
  gdir="$cache/golangci-lint/$want"
  if [ ! -x "$gdir/golangci-lint" ]; then
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"; case "$(uname -m)" in arm64|aarch64) arch=arm64;; *) arch=amd64;; esac
    sha="$(awk -v k="$os-$arch" '$1==k{print $2}' "$dir/golangci-lint.lock")"
    [ -n "$sha" ] || die "golangci-lint.lock has no checksum for $os-$arch"
    name="golangci-lint-$want-$os-$arch"; g="$(mktemp -d)"
    curl -fsSL "${GOLANGCI_DL_BASE:-https://github.com/golangci/golangci-lint/releases/download}/v$want/$name.tar.gz" -o "$g/gcl.tgz" \
      || die "could not download golangci-lint $want"
    got="$( (shasum -a 256 "$g/gcl.tgz" 2>/dev/null || sha256sum "$g/gcl.tgz") | cut -d' ' -f1)"
    [ "$got" = "$sha" ] || { rm -rf "$g"; die "golangci-lint $want checksum mismatch (got $got); nothing installed"; }
    tar -xzf "$g/gcl.tgz" -C "$g" && mkdir -p "$gdir" && mv "$g/$name/golangci-lint" "$gdir/" && rm -rf "$g"
    echo "go-ci local: installed golangci-lint $want into $gdir"
  fi
  PATH="$gdir:$PATH"; export PATH
fi

# Worktrees of one module share import paths, so one golangci-lint cache
# reports findings from other worktrees' files: each checkout gets its own.
if [ -z "${GOLANGCI_LINT_CACHE:-}" ]; then
  key="$(printf '%s' "$PWD" | (shasum -a 256 2>/dev/null || sha256sum) | cut -c1-16)"
  export GOLANGCI_LINT_CACHE="$cache/golangci-lint-cache/$key"
fi

# Go: the version CI's actions/setup-go installs from go-version-file: go.mod.
# Its rule: the toolchain directive if present, else the go directive, used as
# is when it names a patch and resolved to that minor's newest stable patch when
# it does not. GOTOOLCHAIN makes the go command on PATH fetch and run exactly
# that version (once; Go caches it). A GOTOOLCHAIN you set is kept.
[ -f go.mod ] || die "no go.mod here: run from the module root"
if [ -z "${GOTOOLCHAIN:-}" ]; then
  gov="$(awk '$1=="toolchain"{sub(/^go/,"",$2); print $2; exit}' go.mod)"
  [ -n "$gov" ] || gov="$(awk '$1=="go"{print $2; exit}' go.mod)"
  [ -n "$gov" ] || die "go.mod has no go directive"
  case "$gov" in
    *.*.*) ;;
    *)
      # A bare minor: newest stable patch from go.dev, cached so later runs work
      # offline (a fetch failure falls back to the cached answer).
      rdir="$cache/go"; rfile="$rdir/$gov"
      json="$(curl -fsSL "${GO_DL_JSON:-https://go.dev/dl/?mode=json&include=all}" 2>/dev/null || true)"
      patch="$(printf '%s' "$json" | grep -oE "\"go${gov//./\\.}\.[0-9]+\"" | tr -d '"' | sed 's/^go//' | sort -t. -k3,3n | tail -1 || true)"
      if [ -n "$patch" ]; then
        mkdir -p "$rdir" && printf '%s\n' "$patch" > "$rfile"
      elif [ -f "$rfile" ]; then
        patch="$(cat "$rfile")"
      else
        die "cannot resolve go $gov to a patch release (go.dev unreachable, nothing cached)"
      fi
      gov="$patch" ;;
  esac
  export GOTOOLCHAIN="go$gov"
fi

echo "go-ci local: tools-actions $ver, golangci-lint ${want:-as installed}, $GOTOOLCHAIN"
FAMILY_CONFIG="$dir/golangci.yml" exec bash "$dir/ci.sh"
