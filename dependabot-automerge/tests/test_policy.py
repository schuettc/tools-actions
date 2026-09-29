"""Behavioural tests for the dependabot-automerge policy decision.

The policy is proved two ways: directly against ``decide()`` and end-to-end by
running ``automerge_policy.py`` as the action runs it (values only through env,
exit code drives merge/skip). The policy table mirrors fetch-metadata's parser:

* a single-dependency docker DIGEST PR yields update-type ``''`` (ungrouped, so
  an empty type is exactly a digest bump);
* a docker TAG bump reports a ``version-update:semver-*`` type;
* patch/minor/major carry their respective ``version-update:semver-*`` types.
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

DEPENDABOT = "dependabot[bot]"
HUMAN = "octocat"

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
    code, reason = decide(actor, utype, eco, DEFAULT_ALLOWED, True)
    merged = code == MERGE
    assert merged is expected, f"{name}: got {code} ({reason})"


def test_major_merges_only_when_explicitly_allowed():
    """The default never merges a major; a caller may opt in via the allow-list."""
    allowed = DEFAULT_ALLOWED | {"version-update:semver-major"}
    code, _ = decide(DEPENDABOT, "version-update:semver-major", "pip", allowed, True)
    assert code == MERGE
    # But with the default list it still waits.
    code, _ = decide(DEPENDABOT, "version-update:semver-major", "pip", DEFAULT_ALLOWED, True)
    assert code == SKIP


def test_docker_digest_can_be_disabled():
    code, _ = decide(DEPENDABOT, "", "docker", DEFAULT_ALLOWED, False)
    assert code == SKIP
    code, _ = decide(DEPENDABOT, "", "docker", DEFAULT_ALLOWED, True)
    assert code == MERGE


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    full = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=full,
        capture_output=True,
        text=True,
    )


DEFAULT_ENV = {
    "ALLOWED_UPDATE_TYPES": "version-update:semver-patch version-update:semver-minor",
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
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "npm",
            "UPDATE_TYPE": "version-update:semver-patch",
            "ALLOWED_UPDATE_TYPES": "version-update:semver-patch",
            "ALLOW_DOCKER_DIGEST": "yes",
        }
    )
    assert result.returncode == MISUSE
    assert "allow-docker-digest must be true|false" in result.stderr


def test_allowed_update_types_accepts_commas():
    """The list splits on commas as well as spaces (env is a single string)."""
    result = _run(
        {
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "npm",
            "UPDATE_TYPE": "version-update:semver-minor",
            "ALLOWED_UPDATE_TYPES": "version-update:semver-patch,version-update:semver-minor",
            "ALLOW_DOCKER_DIGEST": "false",
        }
    )
    assert result.returncode == MERGE
