"""Behavioural tests for the dependabot-automerge policy decision.

The policy is proved two ways: directly against ``decide()`` and end-to-end by
running ``automerge_policy.py`` as the action runs it (values only through env,
exit code drives merge/skip). The policy table mirrors fetch-metadata's parser:

* a single-dependency docker DIGEST PR yields update-type ``''`` (ungrouped, so
  an empty type is exactly a digest bump);
* a docker TAG bump reports a ``version-update:semver-*`` type;
* patch/minor/major carry their respective ``version-update:semver-*`` types.

It also proves the restored safety surface (I3/I4): the target-branch guard, the
ecosystem allow-list, and loud rejection of an empty/typo'd ``allowed-update-types``
or ``allowed-ecosystems`` and an empty ``target-branch``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ACTION_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ACTION_DIR / "automerge_policy.py"

sys.path.insert(0, str(ACTION_DIR))
from automerge_policy import MERGE, MISUSE, SKIP, decide  # noqa: E402

# The shipped default allow-list: patch + minor, never major.
DEFAULT_ALLOWED = {
    "version-update:semver-patch",
    "version-update:semver-minor",
}
DEFAULT_ECOSYSTEMS = {"docker", "github-actions", "uv", "pip", "npm"}
TARGET = "dev"

DEPENDABOT = "dependabot[bot]"
HUMAN = "octocat"


def _decide(actor, utype, eco, *, allowed=None, digest=True, ecosystems=None,
            base_ref=TARGET, target=TARGET):
    return decide(
        actor,
        utype,
        eco,
        DEFAULT_ALLOWED if allowed is None else allowed,
        digest,
        DEFAULT_ECOSYSTEMS if ecosystems is None else ecosystems,
        base_ref,
        target,
    )


# (name, actor, ecosystem, update-type, expect_merge)
POLICY_TABLE = [
    ("patch merges", DEPENDABOT, "npm", "version-update:semver-patch", True),
    ("minor merges", DEPENDABOT, "uv", "version-update:semver-minor", True),
    ("major waits", DEPENDABOT, "pip", "version-update:semver-major", False),
    ("docker digest merges", DEPENDABOT, "docker", "", True),
    ("docker tag major waits", DEPENDABOT, "docker", "version-update:semver-major", False),
    ("non-dependabot actor never merges", HUMAN, "npm", "version-update:semver-patch", False),
    # An empty update-type on a NON-docker ecosystem is not a digest bump — wait.
    ("empty update-type on npm waits", DEPENDABOT, "npm", "", False),
]


@pytest.mark.parametrize("name,actor,eco,utype,expected", POLICY_TABLE)
def test_decide_matches_policy(name, actor, eco, utype, expected):
    code, reason = _decide(actor, utype, eco)
    merged = code == MERGE
    assert merged is expected, f"{name}: got {code} ({reason})"


def test_major_merges_only_when_explicitly_allowed():
    """The default never merges a major; a caller may opt in via the allow-list."""
    allowed = DEFAULT_ALLOWED | {"version-update:semver-major"}
    code, _ = _decide(DEPENDABOT, "version-update:semver-major", "pip", allowed=allowed)
    assert code == MERGE
    # But with the default list it still waits.
    code, _ = _decide(DEPENDABOT, "version-update:semver-major", "pip")
    assert code == SKIP


def test_docker_digest_can_be_disabled():
    code, _ = _decide(DEPENDABOT, "", "docker", digest=False)
    assert code == SKIP
    code, _ = _decide(DEPENDABOT, "", "docker", digest=True)
    assert code == MERGE


def test_target_branch_mismatch_never_merges():
    """A PR whose base is not the permitted target-branch waits, even for a
    normally-mergeable patch (I4: restore the source's target-branch guard)."""
    code, reason = _decide(
        DEPENDABOT, "version-update:semver-patch", "npm", base_ref="main", target="dev"
    )
    assert code == SKIP
    assert "target-branch" in reason
    # Matching base still merges.
    code, _ = _decide(
        DEPENDABOT, "version-update:semver-patch", "npm", base_ref="dev", target="dev"
    )
    assert code == MERGE


def test_ecosystem_not_in_allowlist_never_merges():
    code, reason = _decide(
        DEPENDABOT, "version-update:semver-patch", "gomod",
        ecosystems={"docker", "npm"},
    )
    assert code == SKIP
    assert "ecosystem" in reason
    # docker digest for a disallowed ecosystem set also waits.
    code, _ = _decide(DEPENDABOT, "", "docker", ecosystems={"npm"})
    assert code == SKIP


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    full = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=full,
        capture_output=True,
        text=True,
    )


DEFAULT_ENV = {
    "TARGET_BRANCH": TARGET,
    "BASE_REF": TARGET,
    "ALLOWED_UPDATE_TYPES": "version-update:semver-patch version-update:semver-minor",
    "ALLOWED_ECOSYSTEMS": "docker github-actions uv pip npm",
    "ALLOW_DOCKER_DIGEST": "true",
}


@pytest.mark.parametrize("name,actor,eco,utype,expected", POLICY_TABLE)
def test_script_exit_code_drives_merge(name, actor, eco, utype, expected):
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": actor,
            "PACKAGE_ECOSYSTEM": eco,
            "UPDATE_TYPE": utype,
        }
    )
    assert result.returncode in (MERGE, SKIP), result.stderr
    merged = result.returncode == MERGE
    assert merged is expected, f"{name}: exit={result.returncode} out={result.stdout}"
    assert result.stdout.startswith("merge " if expected else "skip ")


def test_script_rejects_bad_allow_docker_digest():
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "npm",
            "UPDATE_TYPE": "version-update:semver-patch",
            "ALLOW_DOCKER_DIGEST": "yes",
        }
    )
    assert result.returncode == MISUSE
    assert "allow-docker-digest must be true|false" in result.stderr


def test_script_rejects_empty_allowed_update_types():
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "npm",
            "UPDATE_TYPE": "version-update:semver-patch",
            "ALLOWED_UPDATE_TYPES": "",
        }
    )
    assert result.returncode == MISUSE
    assert "allowed-update-types must not be empty" in result.stderr


def test_script_rejects_typo_in_allowed_update_types():
    """A misspelled token (missing the version-update: prefix) fails loudly rather
    than quietly turning patch/minor merging off."""
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "npm",
            "UPDATE_TYPE": "version-update:semver-patch",
            "ALLOWED_UPDATE_TYPES": "semver-patch version-update:semver-minor",
        }
    )
    assert result.returncode == MISUSE
    assert "allowed-update-types" in result.stderr


def test_script_rejects_empty_allowed_ecosystems():
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "npm",
            "UPDATE_TYPE": "version-update:semver-patch",
            "ALLOWED_ECOSYSTEMS": "   ",
        }
    )
    assert result.returncode == MISUSE
    assert "allowed-ecosystems must not be empty" in result.stderr


def test_script_rejects_empty_target_branch():
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "npm",
            "UPDATE_TYPE": "version-update:semver-patch",
            "TARGET_BRANCH": "   ",
        }
    )
    assert result.returncode == MISUSE
    assert "target-branch is required" in result.stderr


def test_script_skips_on_target_branch_mismatch():
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "npm",
            "UPDATE_TYPE": "version-update:semver-patch",
            "BASE_REF": "main",
        }
    )
    assert result.returncode == SKIP
    assert result.stdout.startswith("skip ")


def test_allowed_update_types_accepts_commas():
    """The list splits on commas as well as spaces (env is a single string)."""
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "npm",
            "UPDATE_TYPE": "version-update:semver-minor",
            "ALLOWED_UPDATE_TYPES": "version-update:semver-patch,version-update:semver-minor",
            "ALLOW_DOCKER_DIGEST": "false",
        }
    )
    assert result.returncode == MERGE
