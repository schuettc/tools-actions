#!/usr/bin/env bash
# THE STAMP GUARD, run against the built artifact rather than the ldflags we
# meant to pass: each listed binary's `version` must print exactly
# "<name> <version> (<commit>, <date>)". Only the native target can run here.
# A binary that is not listed is never executed (not every binary has a
# `version` command, and running an unknown one can have side effects).
#
# Env: STAMP_CHECK (space- or newline-separated names), VERSION, COMMIT, DATE.
set -euo pipefail
os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in arm64|aarch64) arch=arm64;; x86_64|amd64) arch=amd64;; *) echo "::error::go-release: unknown arch $(uname -m)"; exit 1;; esac
for name in ${STAMP_CHECK:-}; do
  bin="dist/${name}_${os}_${arch}/${name}"
  [ -x "$bin" ] || { echo "::error::go-release: stamp-check: $bin was not built"; exit 1; }
  want="$name $VERSION ($COMMIT, $DATE)"
  got="$("$bin" version)"
  if [ "$got" != "$want" ]; then
    echo "::error::go-release: $bin reports '$got', expected '$want'. Refusing to ship it."; exit 1
  fi
  echo "stamp ok: $got"
done
