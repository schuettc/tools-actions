"""Decide whether a Dependabot PR may be auto-merged.

This is the ONE policy decision for the ``dependabot-automerge`` composite
action. It reads ``dependabot/fetch-metadata`` outputs plus the caller's policy
(all through env, never interpolated into a shell) and answers a single
question: *may this PR arm GitHub-native auto-merge?* It is stdlib-only and runs
under the runner's own ``python3``.

Environment
-----------
* ``ACTOR``                — ``github.actor``; must be ``dependabot[bot]``.
* ``UPDATE_TYPE``          — ``steps.meta.outputs.update-type`` (e.g.
  ``version-update:semver-patch``; empty for a single-dependency docker digest
  bump, per fetch-metadata's parser: ``calculateUpdateType('', 'sha256:…')``
  returns ``''``).
* ``PACKAGE_ECOSYSTEM``    — ``steps.meta.outputs.package-ecosystem``.
* ``ALLOWED_UPDATE_TYPES`` — space/comma-separated fetch-metadata update-types
  that may auto-merge. The default (patch + minor) deliberately OMITS
  ``version-update:semver-major`` so majors wait for human review; a caller may
  add it, but the policy never keys on it implicitly.
* ``ALLOW_DOCKER_DIGEST``  — ``true``/``false``. A docker *digest* bump carries
  an empty update-type; docker is ungrouped (one PR per image) so an empty
  update-type on a docker PR is exactly a digest bump. Docker *tag* bumps always
  report a ``version-update:semver-*`` type, so this rule never catches them.

Output
------
Prints one line ``merge <reason>`` or ``skip <reason>``. Exit code:
``0`` = merge, ``10`` = skip (policy said no), ``1`` = misuse.
"""

from __future__ import annotations

import os
import sys

MERGE = 0
SKIP = 10
MISUSE = 1

# fetch-metadata's parser never assigns this type to an empty-previous digest
# bump; keeping the name here documents the invariant the default policy relies
# on: majors are absent from the default allow-list, so they are never merged
# implicitly.
_MAJOR = "version-update:semver-major"


def _split(value: str) -> list[str]:
    """Split a space/comma-separated list into non-empty tokens."""
    return [tok for tok in value.replace(",", " ").split() if tok]


def decide(
    actor: str,
    update_type: str,
    ecosystem: str,
    allowed_types: set[str],
    allow_docker_digest: bool,
) -> tuple[int, str]:
    """Return ``(exit_code, reason)`` for a single PR.

    A PR merges only when it is Dependabot's own AND either its update-type is
    explicitly allowed, or it is an ungrouped docker digest bump (empty
    update-type) and digest bumps are permitted. Everything else — majors,
    unknown types, non-Dependabot actors — waits.
    """
    if actor != "dependabot[bot]":
        return SKIP, f"actor {actor!r} is not dependabot[bot]"
    if update_type and update_type in allowed_types:
        return MERGE, f"update-type {update_type} is in the allow-list"
    if allow_docker_digest and ecosystem == "docker" and update_type == "":
        return MERGE, "docker digest bump (empty update-type on ungrouped docker)"
    return SKIP, (
        f"update-type {update_type!r} for ecosystem {ecosystem!r} is not in policy"
    )


def main() -> int:
    actor = os.environ.get("ACTOR", "")
    update_type = os.environ.get("UPDATE_TYPE", "")
    ecosystem = os.environ.get("PACKAGE_ECOSYSTEM", "")
    allowed = set(_split(os.environ.get("ALLOWED_UPDATE_TYPES", "")))
    raw = os.environ.get("ALLOW_DOCKER_DIGEST", "true").strip().lower()
    if raw not in ("true", "false"):
        print(
            f"::error::allow-docker-digest must be true|false, got {raw!r}",
            file=sys.stderr,
        )
        return MISUSE
    allow_docker = raw == "true"

    code, reason = decide(actor, update_type, ecosystem, allowed, allow_docker)
    verb = "merge" if code == MERGE else "skip"
    print(f"{verb} {reason}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
