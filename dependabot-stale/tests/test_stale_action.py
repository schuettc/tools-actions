"""Contract + lint tests for the ``dependabot-stale`` composite action.

This is the ported half of the reviewed source's ``stale-dependabot-prs`` job:
a weekly sweep that keeps ONE labelled tracking issue for Dependabot PRs held
open past a threshold. tools-actions ships no copier render layer, so this suite
asserts the shipped action directly, plus the README caller example (extracted
and run through actionlint / a yaml loader):

- ``dependabot-stale/action.yml`` — the composite: it lists open Dependabot PRs,
  opens/updates/closes one labelled tracking issue, never checks out PR code,
  rejects empty/invalid inputs, and every ``run:`` step is strict bash with no
  ``${{ }}`` interpolation and passes shellcheck.
- the README caller example — a real ``schedule`` (+ ``pull_request``) caller with
  least-privilege ``issues: write`` on the stale job, validated by actionlint and
  whose ``with:`` keys and permissions are checked against the real action.

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

# The fixed tracking-issue label carried by the reviewed source.
STALE_LABEL = "dependency-stale"

# The permissions the stale sweep actually needs. A caller example's stale job
# must grant at least these.
REQUIRED_PERMISSIONS = {
    "contents": "read",
    "pull-requests": "read",
    "issues": "write",
}

# The reusable step-variable guard lives beside this test (copied verbatim from
# bump/tests/stepvars.py; bump/ is not modified).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stepvars import undefined_run_vars  # noqa: E402


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _run_steps(doc: dict) -> list[dict]:
    return [s for s in doc["runs"]["steps"] if "run" in s]


def _all_run(doc: dict) -> str:
    return "\n".join(s["run"] for s in _run_steps(doc))


def _fenced(lang: str) -> list[str]:
    pattern = re.compile(rf"```{lang}\n(.*?)```", re.DOTALL)
    return pattern.findall(README.read_text())


def _yaml_callers() -> list[dict]:
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
    assert _load(ACTION_YML)["runs"]["using"] == "composite"


def test_never_checks_out_pr_code():
    assert "actions/checkout" not in ACTION_YML.read_text()


def test_no_uses_steps_at_all():
    # The sweep is pure gh/jq; it pulls in no third-party action.
    doc = _load(ACTION_YML)
    assert [s for s in doc["runs"]["steps"] if "uses" in s] == []


def test_manages_one_labelled_tracking_issue():
    run = _all_run(_load(ACTION_YML))
    # One fixed-label issue, updated in place and closed when none remain.
    assert STALE_LABEL in ACTION_YML.read_text()
    assert "gh issue create" in run
    assert "gh issue edit" in run
    assert "gh issue close" in run
    assert "gh pr list" in run


def test_threshold_flows_from_input_into_env():
    doc = _load(ACTION_YML)
    assert "stale-days" in doc["inputs"]
    # The threshold reaches the sweep as STALE_DAYS env, sourced from the input.
    for step in _run_steps(doc):
        if "gh pr list" in step["run"]:
            assert step["env"].get("STALE_DAYS") == "${{ inputs.stale-days }}"
            break
    else:
        pytest.fail("no sweep step runs gh pr list")


def test_title_and_label_are_inputs():
    inputs = _load(ACTION_YML)["inputs"]
    assert {"github-token", "stale-days", "label", "issue-title"} <= set(inputs)
    assert inputs["label"]["default"] == STALE_LABEL
    assert inputs["issue-title"]["default"] == "Stale Dependabot PRs"


def test_loud_on_gh_errors():
    run = _all_run(_load(ACTION_YML))
    assert "set -euo pipefail" in run
    # The label check captures gh's output first (so a gh failure is
    # distinguishable from an absent label) and fails loudly — it never pipes
    # gh's exit status straight into grep.
    assert "gh label list failed" in run
    assert 'names="$(gh label list' in run
    assert 'grep -qx "$LABEL"' in run


def test_rejects_empty_or_invalid_inputs_loudly():
    raw = ACTION_YML.read_text()
    assert 'if [ -z "$GH_TOKEN" ]' in raw
    assert "github-token is required" in raw
    assert "stale-days must be a positive integer" in raw
    assert "label must not be empty" in raw
    assert "issue-title must not be empty" in raw


def test_every_run_step_is_strict_bash():
    for step in _run_steps(_load(ACTION_YML)):
        assert step.get("shell") == "bash", f"{step.get('name')!r} lacks shell: bash"
        assert "set -euo pipefail" in step["run"], f"{step.get('name')!r} lacks set -euo pipefail"


def test_no_gha_expression_inside_run_scripts():
    for step in _run_steps(_load(ACTION_YML)):
        assert "${{" not in step["run"], (
            f"{step.get('name')!r} interpolates ${{{{ }}}} in run:; pass values through env"
        )


def test_no_project_facts_baked_in():
    # The repo comes from github.repository at run time, not a hardcoded name.
    raw = ACTION_YML.read_text()
    assert "schuettc" not in raw
    assert "${{ github.repository }}" in raw


def test_stepvars_guard_passes():
    findings = undefined_run_vars(ACTION_YML)
    assert findings == [], f"undefined run vars: {findings}"


def test_run_scripts_pass_shellcheck():
    shellcheck = _tool("shellcheck")
    for step in _run_steps(_load(ACTION_YML)):
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


# --- README caller example ----------------------------------------------------


def test_readme_caller_actionlint_clean(tmp_path: Path):
    """Extract each README caller workflow and run it through actionlint. Also
    validate its inputs against the REAL local actions by swapping the pinned
    `uses:` to `./<action>` and linting that under the repo root."""
    runner = _actionlint()
    ignore = ["-ignore", r'label ".+" is unknown']
    for i, block in enumerate(b for b in _fenced("yaml") if "jobs:" in b):
        remote = tmp_path / f"caller-{i}.yml"
        remote.write_text(block)
        r1 = subprocess.run([*runner, *ignore, str(remote)], capture_output=True, text=True)
        assert r1.returncode == 0, f"actionlint (remote) failed:\n{r1.stdout}\n{r1.stderr}"

        local_dir = REPO_ROOT / ".github" / "workflows"
        local_dir.mkdir(parents=True, exist_ok=True)
        local = local_dir / f"_readme_stale_caller_{i}.yml"
        swapped = re.sub(
            r"schuettc/tools-actions/(dependabot-stale|dependabot-automerge)@v\S+",
            r"./\1",
            block,
        )
        local.write_text(swapped)
        try:
            r2 = subprocess.run(
                [*runner, *ignore, str(local)], cwd=REPO_ROOT, capture_output=True, text=True
            )
            assert r2.returncode == 0, (
                f"actionlint (local action inputs) failed:\n{r2.stdout}\n{r2.stderr}"
            )
        finally:
            local.unlink()


def test_readme_caller_has_schedule_and_pull_request_triggers():
    for doc in _yaml_callers():
        triggers = doc.get("on", doc.get(True))
        assert "schedule" in triggers, "the sweep caller must have a weekly schedule trigger"
        assert "pull_request" in triggers
        assert "pull_request_target" not in triggers


def test_readme_stale_job_permissions_cover_needs():
    for doc in _yaml_callers():
        for job in doc["jobs"].values():
            steps = job.get("steps", [])
            if not any("dependabot-stale" in (s.get("uses", "")) for s in steps):
                continue
            perms = job.get("permissions", {})
            for key, level in REQUIRED_PERMISSIONS.items():
                assert perms.get(key) == level, (
                    f"stale caller job must grant {key}: {level}, got {perms.get(key)!r}"
                )


def test_readme_caller_with_keys_are_valid_inputs():
    action_inputs = set(_load(ACTION_YML)["inputs"])
    for doc in _yaml_callers():
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                if "dependabot-stale" not in step.get("uses", ""):
                    continue
                with_keys = set((step.get("with") or {}))
                assert with_keys <= action_inputs, f"unknown with: keys {with_keys - action_inputs}"
                assert "github-token" in with_keys, "caller must pass github-token"


def test_readme_caller_runner_is_pinned():
    for doc in _yaml_callers():
        raw = yaml.safe_dump(doc)
        assert "-latest" not in raw, "caller runner must be a pinned image, never *-latest"


def test_readme_pins_this_repo_at_release_tag():
    version = (REPO_ROOT / "VERSION").read_text().strip()
    pins = re.findall(r"schuettc/tools-actions/\S+@(v\d\S*)", README.read_text())
    assert pins, "README must show at least one pinned caller example"
    for pin in pins:
        assert pin == f"v{version}", f"README pin {pin} != v{version}"
