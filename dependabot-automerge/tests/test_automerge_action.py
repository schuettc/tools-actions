"""Contract + lint tests for the ``dependabot-automerge`` composite action.

tools-actions ships no copier render layer, so this suite asserts the shipped
action directly, plus the examples in its README (extracted and run through
actionlint / a yaml loader — there is no hand-written test-only caller):

- ``dependabot-automerge/action.yml`` — the composite that carries
  ``automerge_policy.py`` and invokes it via ``$GITHUB_ACTION_PATH``: it pins
  ``dependabot/fetch-metadata`` exactly, arms native auto-merge (``gh pr merge
  --auto``, never a direct merge), never checks out PR code, re-checks the actor,
  rejects an empty token, and every ``run:`` step is strict bash with no
  ``${{ }}`` interpolation and passes shellcheck.
- the README caller example — a real ``pull_request`` caller (never
  ``pull_request_target``; the actor gate; least-privilege write permissions)
  that actionlint validates and whose ``with:`` keys and permissions are checked
  against the action's real input surface and needs.
- the README ``dependabot.yml`` example — loaded and sanity-checked.

``actionlint`` and ``shellcheck`` MUST run — they never skip. A binary is
preferred, else ``uvx --from …``; if neither exists the test fails (the CI job
installs the same pinned tools so this always runs).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ACTION_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ACTION_DIR.parents[0]
ACTION_YML = ACTION_DIR / "action.yml"
README = ACTION_DIR / "README.md"

# The exact v2 tag of dependabot/fetch-metadata (matches the reviewed source).
FETCH_METADATA_PIN = "dependabot/fetch-metadata@v2.5.0"

# The permissions this action's steps actually need (documented in the README's
# security model). A caller example must grant at least these.
REQUIRED_PERMISSIONS = {"contents": "write", "pull-requests": "write"}

# The reusable step-variable guard lives beside this test so other actions can
# adopt it; import it straight from there. (Copied verbatim from
# bump/tests/stepvars.py; bump/ is not modified.)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stepvars import undefined_run_vars  # noqa: E402


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _run_steps(doc: dict) -> list[dict]:
    return [s for s in doc["runs"]["steps"] if "run" in s]


def _fenced(lang: str) -> list[str]:
    """Every fenced ```<lang> block in the README, in order."""
    pattern = re.compile(rf"```{lang}\n(.*?)```", re.DOTALL)
    return pattern.findall(README.read_text())


def _yaml_callers() -> list[dict]:
    """README ```yaml blocks that are workflows (have `jobs:`)."""
    out = []
    for block in _fenced("yaml"):
        doc = yaml.safe_load(block)
        if isinstance(doc, dict) and "jobs" in doc:
            out.append(doc)
    assert out, "README must contain at least one caller workflow example"
    return out


def _tool(name: str, *uvx: str) -> list[str]:
    binary = shutil.which(name)
    if binary:
        return [binary]
    if uvx and shutil.which("uvx"):
        return ["uvx", *uvx]
    pytest.fail(f"{name} is not available — it must run, not skip")


def _actionlint() -> list[str]:
    return _tool("actionlint", "--from", "actionlint-py", "actionlint")


# --- action.yml contract ------------------------------------------------------


def test_is_a_composite_action():
    doc = _load(ACTION_YML)
    assert doc["runs"]["using"] == "composite"


def test_fetch_metadata_pinned_to_exact_v2_tag():
    doc = _load(ACTION_YML)
    uses = [s["uses"] for s in doc["runs"]["steps"] if "uses" in s]
    assert uses == [FETCH_METADATA_PIN], uses
    for u in uses:
        assert re.search(r"@v\d+\.\d+\.\d+$", u), f"{u} is not pinned to an exact vX.Y.Z tag"


def test_never_checks_out_pr_code():
    assert "actions/checkout" not in ACTION_YML.read_text()


def test_uses_native_auto_merge_never_direct():
    # Scan the run: scripts only (prose in descriptions is not a command). Every
    # `gh pr merge` invocation must arm --auto, never a direct merge.
    doc = _load(ACTION_YML)
    invocations = [
        line
        for step in _run_steps(doc)
        for line in step["run"].splitlines()
        if "gh pr merge" in line
    ]
    assert invocations, "the action must run gh pr merge"
    for line in invocations:
        assert "--auto" in line, f"direct merge (no --auto): {line!r}"


def test_rechecks_dependabot_actor():
    raw = ACTION_YML.read_text()
    assert "dependabot[bot]" in raw
    assert "github.actor" in raw


def test_rejects_empty_token_loudly():
    raw = ACTION_YML.read_text()
    assert 'if [ -z "$GH_TOKEN" ]' in raw
    assert "github-token is required" in raw


def test_every_run_step_is_strict_bash():
    doc = _load(ACTION_YML)
    for step in _run_steps(doc):
        assert step.get("shell") == "bash", f"{step.get('name')!r} lacks shell: bash"
        assert "set -euo pipefail" in step["run"], f"{step.get('name')!r} lacks set -euo pipefail"


def test_no_gha_expression_inside_run_scripts():
    doc = _load(ACTION_YML)
    for step in _run_steps(doc):
        assert "${{" not in step["run"], (
            f"{step.get('name')!r} interpolates ${{{{ }}}} in run:; pass values through env"
        )


def test_policy_values_are_inputs_not_project_facts():
    doc = _load(ACTION_YML)
    inputs = doc["inputs"]
    assert set(inputs) >= {
        "github-token",
        "merge-method",
        "allowed-update-types",
        "allow-docker-digest",
    }
    # No calling-project fact (repo name/org/branch/package/check name) baked in.
    assert "schuettc" not in ACTION_YML.read_text()


def test_stepvars_guard_passes():
    findings = undefined_run_vars(ACTION_YML)
    assert findings == [], f"undefined run vars: {findings}"


def test_run_scripts_pass_shellcheck():
    """Extract each run: script from action.yml and shellcheck it (SC would not
    otherwise see scripts embedded in yaml)."""
    shellcheck = _tool("shellcheck")
    doc = _load(ACTION_YML)
    for step in _run_steps(doc):
        script = "#!/usr/bin/env bash\n" + step["run"]
        result = subprocess.run(
            [*shellcheck, "-s", "bash", "-"],
            input=script,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"shellcheck failed for step {step.get('name')!r}:\n{result.stdout}\n{result.stderr}"
        )


# --- README examples ----------------------------------------------------------


def test_readme_caller_actionlint_clean(tmp_path: Path):
    """Extract each README caller workflow and run it through actionlint. Also
    validate its inputs against the REAL local action by swapping the pinned
    `uses:` to `./dependabot-automerge` and linting that under the repo root."""
    runner = _actionlint()
    ignore = ["-ignore", r'label ".+" is unknown']
    for i, block in enumerate(b for b in _fenced("yaml") if "jobs:" in b):
        # As written (remote pin): syntax + expression check.
        remote = tmp_path / f"caller-{i}.yml"
        remote.write_text(block)
        r1 = subprocess.run(
            [*runner, *ignore, str(remote)], capture_output=True, text=True
        )
        assert r1.returncode == 0, f"actionlint (remote) failed:\n{r1.stdout}\n{r1.stderr}"

        # Local variant: validates the with: keys against the real action.yml.
        local_dir = REPO_ROOT / ".github" / "workflows"
        local_dir.mkdir(parents=True, exist_ok=True)
        local = local_dir / f"_readme_caller_{i}.yml"
        # Swap BOTH sibling actions (the combined caller pairs auto-merge with
            # the weekly stale sweep) so actionlint validates each with: block
            # against the real local action.yml.
        local.write_text(
            re.sub(
                r"schuettc/tools-actions/(dependabot-automerge|dependabot-stale)@v\S+",
                r"./\1",
                block,
            )
        )
        try:
            r2 = subprocess.run(
                [*runner, *ignore, str(local)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            assert r2.returncode == 0, (
                f"actionlint (local action inputs) failed:\n{r2.stdout}\n{r2.stderr}"
            )
        finally:
            local.unlink()


def test_readme_caller_never_uses_pull_request_target():
    for doc in _yaml_callers():
        triggers = doc.get("on", doc.get(True))
        assert "pull_request_target" not in triggers
        assert "pull_request" in triggers


def test_readme_caller_permissions_cover_needs():
    """Every caller job that uses this action must grant at least the permissions
    the action needs — a called action cannot widen the caller's token."""
    for doc in _yaml_callers():
        # Top-level permissions must be least-privilege ({}), so the job grants.
        for job in doc["jobs"].values():
            steps = job.get("steps", [])
            if not any("dependabot-automerge" in (s.get("uses", "")) for s in steps):
                continue
            perms = job.get("permissions", {})
            for key, level in REQUIRED_PERMISSIONS.items():
                assert perms.get(key) == level, (
                    f"caller job must grant {key}: {level}, got {perms.get(key)!r}"
                )


def test_readme_caller_with_keys_are_valid_inputs():
    action_inputs = set(_load(ACTION_YML)["inputs"])
    for doc in _yaml_callers():
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                if "dependabot-automerge" not in step.get("uses", ""):
                    continue
                with_keys = set((step.get("with") or {}))
                assert with_keys <= action_inputs, (
                    f"unknown with: keys {with_keys - action_inputs}"
                )
                assert "github-token" in with_keys, "caller must pass github-token"


def test_readme_caller_runner_is_pinned():
    for doc in _yaml_callers():
        raw = yaml.safe_dump(doc)
        assert "-latest" not in raw, "caller runner must be a pinned image, never *-latest"


def test_readme_dependabot_yml_example_loads():
    # The dependabot.yml example is documentation; prove it is valid yaml and
    # shaped right (v2, docker ungrouped so a digest bump has an empty type).
    docs = [b for b in _fenced("yaml") if "package-ecosystem" in b]
    assert docs, "README must contain a dependabot.yml example"
    doc = yaml.safe_load(docs[0])
    assert doc["version"] == 2
    by_eco = {u["package-ecosystem"]: u for u in doc["updates"]}
    assert "groups" not in by_eco["docker"], "docker must NOT be grouped"
    assert by_eco["github-actions"]["groups"]["minor-and-patch"]["patterns"] == ["*"]
    assert "semver-major-days" not in by_eco["docker"]["cooldown"]


def test_readme_pins_this_repo_at_release_tag():
    version = (REPO_ROOT / "VERSION").read_text().strip()
    # Require a digit after @v so the prose placeholder `@vX.Y.Z` is not counted.
    pins = re.findall(r"schuettc/tools-actions/\S+@(v\d\S*)", README.read_text())
    assert pins, "README must show at least one pinned caller example"
    for pin in pins:
        assert pin == f"v{version}", f"README pin {pin} != v{version}"
