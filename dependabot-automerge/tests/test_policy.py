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

# Real Dependabot docker DIGEST bump PR titles (ungrouped: one image per PR).
# fetch-metadata@v2.5.0 leaves BOTH update-type and new-version empty for these
# (its version regex needs a leading digit, but the version is wrapped in
# backticks), so the title is the only signal. Captured live from public repos
# (see the PR-#17 report for URLs):
#   Bump node from `2fe369e` to `0e0ff40`   -> bl0rb/OpenVizPilot#27
#   Bump golang from `cf6fca6` to `8a5910f` -> cantabular/hanoverd#270
#   Bump python from `cad9a2c` to `51dafde` -> andypitcher/IoT_Sentinel#47
#   Bump debian from `0463431` to `5bc3287` -> vascoguita/raspios-docker#55
# The commit-message body is identical but prefixed with "Bumps" and suffixed
# with a period: e.g. "Bumps node from `2fe369e` to `0e0ff40`.".
REAL_DIGEST_TITLES = [
    "Bump node from `2fe369e` to `0e0ff40`",
    "Bump golang from `cf6fca6` to `8a5910f`",
    "Bump python from `cad9a2c` to `51dafde`",
    "Bump debian from `0463431` to `5bc3287`",
]
DIGEST_TITLE = REAL_DIGEST_TITLES[0]


def _decide(actor, utype, eco, *, allowed=None, digest=True, ecosystems=None,
            base_ref=TARGET, target=TARGET, new_version="", title=""):
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
        title,
    )


# (name, actor, ecosystem, update-type, new-version, title, expect_merge)
POLICY_TABLE = [
    ("patch merges", DEPENDABOT, "npm", "version-update:semver-patch", "1.2.3", "", True),
    ("minor merges", DEPENDABOT, "uv", "version-update:semver-minor", "1.3.0", "", True),
    ("major waits", DEPENDABOT, "pip", "version-update:semver-major", "2.0.0", "", False),
    # A real digest bump: empty update-type AND empty new-version; the title
    # carries the `<hex>` -> `<hex>` shape.
    ("docker digest merges", DEPENDABOT, "docker", "", "", DIGEST_TITLE, True),
    ("docker tag major waits", DEPENDABOT, "docker", "version-update:semver-major", "2.0.0", "", False),
    ("non-dependabot actor never merges", HUMAN, "npm", "version-update:semver-patch", "1.2.3", "", False),
    # An empty update-type on a NON-docker ecosystem is not a digest bump — wait.
    ("empty update-type on npm waits", DEPENDABOT, "npm", "", "1.2.3", "", False),
]


@pytest.mark.parametrize("name,actor,eco,utype,nver,title,expected", POLICY_TABLE)
def test_decide_matches_policy(name, actor, eco, utype, nver, title, expected):
    code, reason = _decide(actor, utype, eco, new_version=nver, title=title)
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
    code, _ = _decide(DEPENDABOT, "", "docker", digest=False, title=DIGEST_TITLE)
    assert code == SKIP
    code, _ = _decide(DEPENDABOT, "", "docker", digest=True, title=DIGEST_TITLE)
    assert code == MERGE


@pytest.mark.parametrize("title", REAL_DIGEST_TITLES)
def test_every_real_digest_title_merges(title):
    """Each real Dependabot docker digest title (empty update-type AND empty
    new-version) is recognised as a digest bump and merges."""
    code, reason = _decide(DEPENDABOT, "", "docker", new_version="", title=title)
    assert code == MERGE, f"{title!r}: got {code} ({reason})"


def test_conventional_commit_prefixed_and_pathed_digest_titles_merge():
    """A conventional-commit prefix and a trailing `in <path>` (both real
    Dependabot title shapes) still merge."""
    for title in (
        "chore(deps): bump node from `2fe369e` to `0e0ff40`",
        "build(deps): Bump golang from `cf6fca6` to `8a5910f` in /docker",
        "Bump python from `cad9a2c` to `51dafde` in /images/api",
    ):
        code, reason = _decide(DEPENDABOT, "", "docker", new_version="", title=title)
        assert code == MERGE, f"{title!r}: got {code} ({reason})"


@pytest.mark.parametrize(
    "new_version,title",
    [
        # An empty update-type with a NON-empty new-version is not a digest bump
        # (real digest bumps leave new-version empty) — fail loud.
        ("1.2.3", DIGEST_TITLE),
        ("sha256:" + "a" * 64, DIGEST_TITLE),
        # A docker TAG change fetch-metadata can't parse: title has no hex digest.
        ("", "Bump python from `bookworm` to `trixie`"),
        ("", "Bump node from 18 to 20"),
        # An unparsed tag-style update with an empty title, or a non-Dependabot
        # shaped title, must not be mistaken for a digest bump.
        ("", ""),
        ("", "Update the base image"),
        ("", "Merge branch 'main' into dev"),
    ],
)
def test_docker_empty_update_type_without_digest_title_fails_loud(new_version, title):
    """An empty docker update-type that is not a recognisable digest bump (a
    non-empty new-version, or a title lacking the `<hex>` -> `<hex>` shape) must
    fail loudly (MISUSE), never fall through to a silent merge."""
    code, reason = _decide(DEPENDABOT, "", "docker", new_version=new_version, title=title)
    assert code == MISUSE, f"expected MISUSE for {(new_version, title)!r}, got {code}: {reason}"
    assert code != MERGE


def test_tag_style_unparsed_update_fails_loudly():
    """The reviewer's specific case: a docker tag change (`bookworm` -> `trixie`)
    that fetch-metadata cannot classify (empty update-type + empty new-version)
    must fail loudly, NOT be treated as a digest bump."""
    code, reason = _decide(
        DEPENDABOT, "", "docker", new_version="", title="Bump python from `bookworm` to `trixie`"
    )
    assert code == MISUSE, reason


def test_injection_shaped_title_cannot_merge():
    """A hostile title (quotes, $(), backticks, newlines) is read via env only and
    can never be a valid digest bump — it fails loudly, never merges."""
    for title in (
        "Bump node from `2fe369e` to `0e0ff40`; $(rm -rf /)",
        "Bump node from `2fe369e` to `0e0ff40`\nmalicious: true",
        "$(touch /tmp/pwned)",
        "'; rm -rf / #",
        "Bump node from `2fe369e` to `0e0ff40` && curl evil",
        "`id`",
    ):
        code, reason = _decide(DEPENDABOT, "", "docker", new_version="", title=title)
        assert code == MISUSE, f"{title!r} unexpectedly {code}: {reason}"
        assert code != MERGE


def test_actor_gate_still_applies_to_a_valid_digest_title():
    """Even with a perfectly-valid digest title, a non-Dependabot actor never
    merges: the actor gate precedes any title use."""
    code, reason = _decide(HUMAN, "", "docker", new_version="", title=DIGEST_TITLE)
    assert code == SKIP, reason
    assert "dependabot[bot]" in reason


def test_docker_empty_update_type_with_real_digest_merges():
    code, reason = _decide(DEPENDABOT, "", "docker", new_version="", title=DIGEST_TITLE)
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
    code, _ = _decide(DEPENDABOT, "", "docker", ecosystems={"npm"}, title=DIGEST_TITLE)
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


@pytest.mark.parametrize("name,actor,eco,utype,nver,title,expected", POLICY_TABLE)
def test_script_exit_code_drives_merge(name, actor, eco, utype, nver, title, expected):
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": actor,
            "PACKAGE_ECOSYSTEM": eco,
            "UPDATE_TYPE": utype,
            "NEW_VERSION": nver,
            "TITLE": title,
        }
    )
    assert result.returncode in (MERGE, SKIP), result.stderr
    merged = result.returncode == MERGE
    assert merged is expected, f"{name}: exit={result.returncode} out={result.stdout}"
    assert result.stdout.startswith("merge " if expected else "skip ")


@pytest.mark.parametrize("title", REAL_DIGEST_TITLES)
def test_script_every_real_digest_title_merges(title):
    """End-to-end via the script: each real docker digest title (empty update-type
    AND empty new-version) merges (exit 0)."""
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "docker",
            "UPDATE_TYPE": "",
            "NEW_VERSION": "",
            "TITLE": title,
        }
    )
    assert result.returncode == MERGE, result.stderr
    assert result.stdout.startswith("merge ")


def test_script_docker_empty_update_type_non_digest_fails_loud():
    """End-to-end: a docker PR with an empty update-type whose title is a tag-style
    change (no hex digest) fails loudly (::error::, exit 1) and never merges."""
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "docker",
            "UPDATE_TYPE": "",
            "NEW_VERSION": "",
            "TITLE": "Bump python from `bookworm` to `trixie`",
        }
    )
    assert result.returncode == MISUSE
    assert result.returncode != MERGE
    assert "::error::" in result.stderr
    assert "not a recognisable digest bump" in result.stderr
    assert not result.stdout.startswith("merge ")


def test_script_injection_shaped_title_cannot_break_or_merge():
    """A hostile title passed through env (quotes, $(), backticks, newlines) can
    neither break the script nor be classified as a digest bump."""
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "docker",
            "UPDATE_TYPE": "",
            "NEW_VERSION": "",
            "TITLE": "Bump node from `2fe369e` to `0e0ff40`; $(rm -rf /)\n`id`",
        }
    )
    assert result.returncode == MISUSE
    assert result.returncode != MERGE
    assert not result.stdout.startswith("merge ")


def test_script_docker_empty_update_type_real_digest_merges():
    result = _run(
        {
            **DEFAULT_ENV,
            "ACTOR": DEPENDABOT,
            "PACKAGE_ECOSYSTEM": "docker",
            "UPDATE_TYPE": "",
            "NEW_VERSION": "",
            "TITLE": DIGEST_TITLE,
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
