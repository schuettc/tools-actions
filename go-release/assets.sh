#!/usr/bin/env bash
# Collect caller-supplied defaults plus EXTRA_ASSETS globs from the repo root.
# Sets assets (unique exact paths, first occurrence wins) and extras (expanded
# extra paths for their sidecar generation). Callers enable nullglob first.
collect_assets() {
  local g f previous duplicate
  extras=()
  for g in ${EXTRA_ASSETS:-}; do
    for f in $g; do
      case "$f" in
        dist/*) extras+=("$f");;
        *) echo "::error::go-release: extra asset $f is not in dist/"; return 1;;
      esac
    done
  done
  assets=()
  for f in "$@" ${extras[@]+"${extras[@]}"}; do
    duplicate=false
    for previous in ${assets[@]+"${assets[@]}"}; do
      if [ "$f" = "$previous" ]; then duplicate=true; break; fi
    done
    [ "$duplicate" = true ] || assets+=("$f")
  done
}
