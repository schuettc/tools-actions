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
* ``BASE_REF``             — ``github.event.pull_request.base.ref``; the branch
  the PR targets. Must equal ``TARGET_BRANCH`` or the PR is skipped (the source
  restricted auto-merge to the integration branch; this restores that guard).
* ``TARGET_BRANCH``        — the one branch Dependabot PRs may auto-merge into
  (an input; the caller passes e.g. ``dev``). Required, non-empty.
* ``ALLOWED_UPDATE_TYPES`` — space/comma-separated fetch-metadata update-types
  that may auto-merge. The default (patch + minor) deliberately OMITS
  ``version-update:semver-major`` so majors wait for human review; a caller may
  add it, but the policy never keys on it implicitly. Must be non-empty and every
  token must be one of ``version-update:semver-{patch,minor,major}`` — an empty
  list or a typo (``semver-patch``) is a misuse, not a silent no-op.
* ``ALLOWED_ECOSYSTEMS``   — space/comma-separated package-ecosystems that may
  auto-merge (the source allowed docker, github-actions, uv, pip, npm). A PR
  whose ecosystem is not in this list is skipped. Empty is a misuse.
* ``ALLOW_DOCKER_DIGEST``  — ``true``/``false``. A docker *digest* bump carries
  an empty update-type; docker is ungrouped (one PR per image) so an empty
  update-type on a docker PR whose ``NEW_VERSION`` is a ``sha256:`` digest is
  exactly a digest bump. A docker PR with an empty update-type whose new-version
  is NOT a digest fails loudly (no fail-open). Docker *tag* bumps always report a
  ``version-update:semver-*`` type, so this rule never catches them.

Output
------
Prints one line ``merge <reason>`` or ``skip <reason>``. Exit code:
``0`` = merge, ``10`` = skip (policy said no), ``1`` = misuse (a bad input; the
step fails loudly rather than defaulting to a merge or a silent skip).
"""

from __future__ import annotations

import os
import re
import sys

MERGE = 0
SKIP = 10
MISUSE = 1

# A docker digest bump carries an empty update-type, but so would a docker PR for
# which fetch-metadata failed to classify the change. We only treat an empty
# docker update-type as a digest bump when the new version actually looks like a
# digest (an OCI content digest: the ``sha256:`` prefix + 64 lowercase hex). Any
# other empty docker update-type fails loudly rather than silently merging.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# The only update-types fetch-metadata's parser emits (besides the empty digest
# type). ``allowed-update-types`` tokens must come from this set — a typo like
# ``semver-patch`` (missing the ``version-update:`` prefix) is rejected loudly so
# it can never quietly turn patch/minor merging off.
_VALID_UPDATE_TYPES = frozenset(
    {
        "version-update:semver-patch",
        "version-update:semver-minor",
        "version-update:semver-major",
    }
)


def _split(value: str) -> list[str]:
    """Split a space/comma-separated list into non-empty tokens."""
    return [tok for tok in value.replace(",", " ").split() if tok]


def decide(
    actor: str,
    update_type: str,
    ecosystem: str,
    allowed_types: set[str],
    allow_docker_digest: bool,
    allowed_ecosystems: set[str],
    base_ref: str,
    target_branch: str,
    new_version: str = "",
) -> tuple[int, str]:
    """Return ``(exit_code, reason)`` for a single PR.

    A PR merges only when it is Dependabot's own, targets the one permitted
    branch, is in an allowed ecosystem, AND either its update-type is explicitly
    allowed or it is an ungrouped docker digest bump (empty update-type whose
    ``new-version`` looks like a digest) and digest bumps are permitted.
    Everything else — majors, unknown types, a PR against the wrong branch or
    from a disallowed ecosystem — waits.

    No silent fail-open: a docker PR with an empty update-type whose new-version
    does NOT look like a digest returns ``MISUSE`` so the step fails loudly
    rather than merging an unclassified change.
    """
    if actor != "dependabot[bot]":
        return SKIP, f"actor {actor!r} is not dependabot[bot]"
    if base_ref != target_branch:
        return SKIP, (
            f"PR base {base_ref!r} is not the permitted target-branch "
            f"{target_branch!r}"
        )
    if ecosystem not in allowed_ecosystems:
        return SKIP, (
            f"ecosystem {ecosystem!r} is not in the allowed-ecosystems list"
        )
    if update_type and update_type in allowed_types:
        return MERGE, f"update-type {update_type} is in the allow-list"
    if allow_docker_digest and ecosystem == "docker" and update_type == "":
        if _DIGEST_RE.match(new_version):
            return MERGE, (
                f"docker digest bump (empty update-type, new-version {new_version} "
                "is a sha256 digest)"
            )
        return MISUSE, (
            f"docker PR has an empty update-type but new-version {new_version!r} "
            "is not a sha256 digest (expected 'sha256:' + 64 hex); refusing to "
            "merge an unclassified docker change rather than fail open"
        )
    return SKIP, (
        f"update-type {update_type!r} for ecosystem {ecosystem!r} is not in policy"
    )


def _load_allowed_update_types() -> set[str]:
    """Parse + validate ``ALLOWED_UPDATE_TYPES``; raise ``ValueError`` on misuse."""
    tokens = _split(os.environ.get("ALLOWED_UPDATE_TYPES", ""))
    if not tokens:
        raise ValueError(
            "allowed-update-types must not be empty (it is the merge allow-list; "
            "an empty list would silently disable all auto-merge)"
        )
    bad = sorted(t for t in tokens if t not in _VALID_UPDATE_TYPES)
    if bad:
        raise ValueError(
            "allowed-update-types tokens must each be one of "
            f"{sorted(_VALID_UPDATE_TYPES)}; got invalid {bad}"
        )
    return set(tokens)


def _load_allowed_ecosystems() -> set[str]:
    """Parse + validate ``ALLOWED_ECOSYSTEMS``; raise ``ValueError`` on misuse."""
    tokens = _split(os.environ.get("ALLOWED_ECOSYSTEMS", ""))
    if not tokens:
        raise ValueError(
            "allowed-ecosystems must not be empty (an empty allowlist would skip "
            "every PR); list the package-ecosystems that may auto-merge"
        )
    return set(tokens)


def main() -> int:
    actor = os.environ.get("ACTOR", "")
    update_type = os.environ.get("UPDATE_TYPE", "")
    ecosystem = os.environ.get("PACKAGE_ECOSYSTEM", "")
    base_ref = os.environ.get("BASE_REF", "")
    new_version = os.environ.get("NEW_VERSION", "").strip()
    target_branch = os.environ.get("TARGET_BRANCH", "").strip()

    if not target_branch:
        print(
            "::error::target-branch is required and must not be empty (the branch "
            "Dependabot PRs may auto-merge into).",
            file=sys.stderr,
        )
        return MISUSE

    try:
        allowed = _load_allowed_update_types()
        allowed_ecosystems = _load_allowed_ecosystems()
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return MISUSE

    raw = os.environ.get("ALLOW_DOCKER_DIGEST", "true").strip().lower()
    if raw not in ("true", "false"):
        print(
            f"::error::allow-docker-digest must be true|false, got {raw!r}",
            file=sys.stderr,
        )
        return MISUSE
    allow_docker = raw == "true"

    code, reason = decide(
        actor,
        update_type,
        ecosystem,
        allowed,
        allow_docker,
        allowed_ecosystems,
        base_ref,
        target_branch,
        new_version,
    )
    if code == MISUSE:
        print(f"::error::{reason}", file=sys.stderr)
        return MISUSE
    verb = "merge" if code == MERGE else "skip"
    print(f"{verb} {reason}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
