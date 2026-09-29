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

# A well-formed OCI content digest (sha256: + 64 lowercase hex): what
# fetch-metadata reports as new-version for a docker digest bump.
DIGEST = "sha256:" + "a" * 64


def _decide(actor, utype, eco, *, allowed=None, digest=True, ecosystems=None,
            base_ref=TARGET, target=TARGET, new_version=""):
    return decide(
        actor,
        utype,
        eco,
        DEFAULT_ALLOWED if allowed is None else allowed,
        digest,
        DEFAULT_ECOSYSTEMS if ecosystems is None else ecosystems,
        base_ref,
        target,
        new_version,
    )


# (name, actor, ecosystem, update-type, new-version, expect_merge)
POLICY_TABLE = [
    ("patch merges", DEPENDABOT, "npm", "version-update:semver-patch", "1.2.3", True),
    ("minor merges", DEPENDABOT, "uv", "version-update:semver-minor", "1.3.0", True),
    ("major waits", DEPENDABOT, "pip", "version-update:semver-major", "2.0.0", False),
    ("docker digest merges", DEPENDABOT, "docker", "", DIGEST, True),
    ("docker tag major waits", DEPENDABOT, "docker", "version-update:semver-major", "2.0.0", False),
    ("non-dependabot actor never merges", HUMAN, "npm", "version-update:semver-patch", "1.2.3", False),
    # An empty update-type on a NON-docker ecosystem is not a digest bump — wait.
    ("empty update-type on npm waits", DEPENDABOT, "npm", "", "1.2.3", False),
]


@pytest.mark.parametrize("name,actor,eco,utype,nver,expected", POLICY_TABLE)
def test_decide_matches_policy(name, actor, eco, utype, nver, expected):
    code, reason = _decide(actor, utype, eco, new_version=nver)
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
    code, _ = _decide(DEPENDABOT, "", "docker", digest=False, new_version=DIGEST)
    assert code == SKIP
    code, _ = _decide(DEPENDABOT, "", "docker", digest=True, new_version=DIGEST)
    assert code == MERGE


@pytest.mark.parametrize(
    "new_version",
    [
        "",
        "1.2.3",
        "latest",
        "sha256:abc",  # too short
        "sha256:" + "a" * 63,  # 63 hex
        "sha256:" + "a" * 65,  # 65 hex
        "sha256:" + "g" * 64,  # non-hex
        "sha256:" + "A" * 64,  # uppercase (docker digests are lowercase)
        "md5:" + "a" * 64,  # wrong algorithm prefix
    ],
)
def test_docker_empty_update_type_without_digest_fails_loud(new_version):
    """An empty docker update-type whose new-version does not look like a digest
    must fail loudly (MISUSE), never fall through to a silent merge."""
    code, reason = _decide(DEPENDABOT, "", "docker", new_version=new_version)
    assert code == MISUSE, f"expected MISUSE for {new_version!r}, got {code}: {reason}"
    assert code != MERGE


def test_docker_empty_update_type_with_real_digest_merges():
    code, reason = _decide(DEPENDABOT, "", "docker", new_version=DIGEST)
    assert code == MERGE, reason


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


@pytest.mark.parametrize("name,actor,eco,utype,nver,expected", POLICY_TABLE)
def test_script_exit_code_drives_merge(name, actor, eco, utype, nver, expected):
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": actor,
            "PACKAGE_ECOSYSTEM": eco,
            "UPDATE_TYPE": utype,
            "NEW_VERSION": nver,
        }
    )
    assert result.returncode in (MERGE, SKIP), result.stderr
    merged = result.returncode == MERGE
    assert merged is expected, f"{name}: exit={result.returncode} out={result.stdout}"
    assert result.stdout.startswith("merge " if expected else "skip ")


def test_script_docker_empty_update_type_non_digest_fails_loud():
    """End-to-end: a docker PR with an empty update-type and a non-digest
    new-version fails loudly (::error::, exit 1) and never merges."""
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "docker",
            "UPDATE_TYPE": "",
            "NEW_VERSION": "1.2.3",
        }
    )
    assert result.returncode == MISUSE
    assert result.returncode != MERGE
    assert "::error::" in result.stderr
    assert "not a sha256 digest" in result.stderr
    assert not result.stdout.startswith("merge ")


def test_script_docker_empty_update_type_real_digest_merges():
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "docker",
            "UPDATE_TYPE": "",
            "NEW_VERSION": DIGEST,
        }
    )
    assert result.returncode == MERGE, result.stderr
    assert result.stdout.startswith("merge ")


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
