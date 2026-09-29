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

import json
import os
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
FIXTURES = ACTION_DIR / "tests" / "fixtures"
# The pinned dependabot/fetch-metadata action.yml at v2.5.0, vendored so the env
# wiring's output names can be checked against the action's own contract.
FETCH_METADATA_ACTION = FIXTURES / "fetch-metadata-v2.5.0-action.yml"

# The exact v2 tag of dependabot/fetch-metadata (matches the reviewed source).
FETCH_METADATA_PIN = "dependabot/fetch-metadata@v2.5.0"

# The permissions each action a README caller uses actually needs (I2: every job
# in each README is checked, not just the auto-merge one).
REQUIRED_PERMISSIONS = {"contents": "write", "pull-requests": "write"}
REQUIRED_PERMISSIONS_BY_ACTION = {
    "dependabot-automerge": {"contents": "write", "pull-requests": "write"},
    "dependabot-stale": {"contents": "read", "pull-requests": "read", "issues": "write"},
}

# The step-variable guard is shared test tooling (testlib/, not an action); its
# own tests live in testlib/tests. Adopt the ONE shared copy.
sys.path.insert(0, str(REPO_ROOT))
from testlib.stepvars import undefined_run_vars  # noqa: E402


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
        "target-branch",
        "merge-method",
        "allowed-update-types",
        "allowed-ecosystems",
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


def test_readme_every_job_permissions_cover_the_action_it_uses():
    """I2: this README's caller pairs the auto-merge job with the stale sweep. A
    job that drops a permission (e.g. the stale job's `issues: write`) or grants
    less than its action needs must fail here — every job in every README caller
    is checked against the action it actually uses, not just auto-merge."""
    checked = 0
    for doc in _yaml_callers():
        # Top-level permissions must be least-privilege so nothing is ambient.
        top = doc.get("permissions")
        assert top == {} or top == "none", (
            f"top-level permissions must be {{}} (nothing ambient), got {top!r}"
        )
        for name, job in doc["jobs"].items():
            steps = job.get("steps", [])
            for action, needs in REQUIRED_PERMISSIONS_BY_ACTION.items():
                if not any(action in (s.get("uses", "")) for s in steps):
                    continue
                perms = job.get("permissions", {})
                for key, level in needs.items():
                    assert perms.get(key) == level, (
                        f"job {name!r} uses {action} and must grant {key}: {level}, "
                        f"got {perms.get(key)!r}"
                    )
                checked += 1
    assert checked >= 2, (
        "expected the README to exercise both the auto-merge and stale jobs"
    )


def test_readme_stale_caller_stale_days_is_a_positive_integer():
    """I2: a stale job in this README with an invalid stale-days (e.g. -3) would
    otherwise pass every test; assert the documented value is a positive int."""
    seen = False
    for doc in _yaml_callers():
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                if "dependabot-stale" not in step.get("uses", ""):
                    continue
                days = (step.get("with") or {}).get("stale-days")
                if days is None:
                    continue
                seen = True
                assert int(str(days)) > 0, f"stale-days must be positive, got {days!r}"
    assert seen, "README must show the stale job passing stale-days"


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
    # I4: every entry targets the integration branch (else Dependabot would target
    # GitHub's default branch and this action would auto-merge into it).
    for eco, u in by_eco.items():
        assert u.get("target-branch") == "dev", (
            f"{eco} entry must set target-branch: dev, got {u.get('target-branch')!r}"
        )
    # I5: the cooldown guarantee — every entry has a cooldown with default-days
    # >= 0, and the SemVer ecosystems also set semver-major-days, so auto-merge
    # never lands a freshly published release without a cooldown.
    semver = {"uv", "pip", "npm"}
    for eco, u in by_eco.items():
        cooldown = u.get("cooldown")
        assert isinstance(cooldown, dict), f"{eco} entry must set a cooldown"
        assert cooldown.get("default-days", -1) >= 0, (
            f"{eco} cooldown.default-days must be >= 0, got {cooldown.get('default-days')!r}"
        )
        if eco in semver:
            assert cooldown.get("semver-major-days", -1) >= 0, (
                f"{eco} is SemVer and must set cooldown.semver-major-days"
            )
    assert "semver-major-days" not in by_eco["docker"]["cooldown"]


def test_readme_pins_this_repo_at_release_tag():
    version = (REPO_ROOT / "VERSION").read_text().strip()
    # Require a digit after @v so the prose placeholder `@vX.Y.Z` is not counted.
    pins = re.findall(r"schuettc/tools-actions/\S+@(v\d\S*)", README.read_text())
    assert pins, "README must show at least one pinned caller example"
    for pin in pins:
        assert pin == f"v{version}", f"README pin {pin} != v{version}"


# --- I1: the merge gate that stops majors, tested for real -------------------
#
# The prose contract tests above can pass while the actual gate is broken (a
# mistyped fetch-metadata output name, or a dropped `if:` condition). These tests
# guard the gate itself: they assert the exact env wiring and the exact `if:`,
# then EXECUTE the real step bodies with a stubbed `gh` so a docker/semver major
# does not merge, a minor does, and a misspelled input fails loudly.


def _step_by_id(doc: dict, step_id: str) -> dict:
    for step in doc["runs"]["steps"]:
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"no step with id={step_id!r} in {ACTION_YML}")


DEFAULT_POLICY_ENV = {
    "ACTOR": "dependabot[bot]",
    "BASE_REF": "dev",
    "TARGET_BRANCH": "dev",
    "ALLOWED_UPDATE_TYPES": "version-update:semver-patch version-update:semver-minor",
    "ALLOWED_ECOSYSTEMS": "docker github-actions uv pip npm",
    "ALLOW_DOCKER_DIGEST": "true",
    "NEW_VERSION": "1.2.3",
}


def _exec_run(step: dict, env: dict, tmp_path: Path, *, stub_bin: str | None = None):
    """Run a step's real `run:` body under bash with a fresh $GITHUB_OUTPUT.

    Returns ``(CompletedProcess, outputs_dict)`` where outputs are parsed from the
    ``name=value`` lines the step wrote to $GITHUB_OUTPUT.
    """
    out_file = tmp_path / f"out_{step.get('id', 'x')}"
    out_file.write_text("")
    path = os.environ.get("PATH", "")
    if stub_bin:
        path = f"{stub_bin}:{path}"
    full = {"PATH": path, "GITHUB_OUTPUT": str(out_file), **env}
    proc = subprocess.run(
        ["bash", "-c", step["run"]],
        env=full,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    outputs: dict[str, str] = {}
    for line in out_file.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            outputs[k] = v
    return proc, outputs


# -- exact env wiring on the policy step (mutating a name breaks this) ---------


def test_policy_step_env_wiring_is_exact():
    policy = _step_by_id(_load(ACTION_YML), "policy")
    env = policy["env"]
    assert env["ACTOR"] == "${{ github.actor }}"
    assert env["UPDATE_TYPE"] == "${{ steps.meta.outputs.update-type }}"
    assert env["PACKAGE_ECOSYSTEM"] == "${{ steps.meta.outputs.package-ecosystem }}"
    assert env["BASE_REF"] == "${{ github.event.pull_request.base.ref }}"
    assert env["TARGET_BRANCH"] == "${{ inputs.target-branch }}"


def test_policy_step_reads_only_real_fetch_metadata_outputs():
    """Every `steps.meta.outputs.<name>` the policy step reads must be a name the
    pinned fetch-metadata@v2.5.0 actually documents (checked against its vendored
    action.yml). A typo like `update-typ` would make the env empty and slip a
    docker major through as a digest bump — caught here."""
    fm = yaml.safe_load(FETCH_METADATA_ACTION.read_text())
    fm_outputs = set(fm["outputs"])
    assert {"update-type", "package-ecosystem", "dependency-type"} <= fm_outputs
    policy = _step_by_id(_load(ACTION_YML), "policy")
    read = re.findall(r"steps\.meta\.outputs\.([a-z-]+)", yaml.safe_dump(policy["env"]))
    assert read, "policy step must read fetch-metadata outputs"
    for name in read:
        assert name in fm_outputs, f"{name!r} is not a fetch-metadata@v2.5.0 output"


# The exact expression the merge step's `if:` must carry. Asserting the WHOLE
# string (not substrings) means mutating `&&` to `||` (which would arm every
# admitted Dependabot PR, majors included) changes the string and fails here.
EXPECTED_MERGE_IF = (
    "${{ steps.guard.outputs.skip != 'true' "
    "&& steps.policy.outputs.merge == 'true' }}"
)


def _normalize_ws(s: str) -> str:
    return " ".join(s.split())


def test_merge_step_if_is_gated_exactly_on_the_policy_decision():
    doc = _load(ACTION_YML)
    merge_if = _step_by_id(doc, "merge")["if"]
    # Assert the WHOLE expression, whitespace-normalized, not substrings. A `&&`
    # -> `||` mutation would survive a substring check but changes this string.
    assert _normalize_ws(merge_if) == _normalize_ws(EXPECTED_MERGE_IF), merge_if
    # fetch-metadata and the policy only run when the guard did not skip.
    assert _step_by_id(doc, "meta")["if"] == "${{ steps.guard.outputs.skip != 'true' }}"
    assert _step_by_id(doc, "policy")["if"] == "${{ steps.guard.outputs.skip != 'true' }}"


def _eval_gha_bool_expr(expr: str, ctx: dict[str, str]) -> bool:
    """Tiny evaluator for the ``&&``/``||``/``==``/``!=`` subset of a GitHub
    Actions ``if:`` expression, enough to evaluate the merge gate over its full
    truth table. It reads the ACTUAL string from action.yml, so a `&&` -> `||`
    mutation is evaluated as an OR and the truth table below diverges."""
    inner = expr.strip()
    if inner.startswith("${{") and inner.endswith("}}"):
        inner = inner[3:-2].strip()

    def _operand(tok: str) -> str:
        tok = tok.strip()
        if len(tok) >= 2 and tok[0] == tok[-1] == "'":
            return tok[1:-1]  # string literal
        if tok not in ctx:
            raise AssertionError(f"unknown operand {tok!r} in {expr!r}")
        return ctx[tok]

    def _cmp(term: str) -> bool:
        term = term.strip()
        if "==" in term:
            lhs, rhs = term.split("==", 1)
            return _operand(lhs) == _operand(rhs)
        if "!=" in term:
            lhs, rhs = term.split("!=", 1)
            return _operand(lhs) != _operand(rhs)
        raise AssertionError(f"unsupported comparison {term!r}")

    # Precedence: && binds tighter than ||, matching GitHub Actions.
    return any(
        all(_cmp(term) for term in clause.split("&&"))
        for clause in inner.split("||")
    )


def test_merge_if_evaluates_correctly_over_its_full_truth_table():
    """Evaluate the REAL merge `if:` from action.yml over every combination of its
    two terms and require it to match the oracle `skip != 'true' AND merge ==
    'true'`. A `&&` -> `||` mutation evaluates as OR and fails at least one row."""
    merge_if = _step_by_id(_load(ACTION_YML), "merge")["if"]
    values = ["true", "false", ""]
    for skip in values:
        for merge in values:
            ctx = {
                "steps.guard.outputs.skip": skip,
                "steps.policy.outputs.merge": merge,
            }
            got = _eval_gha_bool_expr(merge_if, ctx)
            expected = (skip != "true") and (merge == "true")
            assert got is expected, (
                f"merge if evaluated {got} for skip={skip!r} merge={merge!r}, "
                f"expected {expected}"
            )


def test_merge_if_truth_table_catches_the_and_to_or_mutation():
    """Guard the guard: prove the truth-table oracle would REJECT the mutated
    expression (`&&` -> `||`), so item-1's evaluation test genuinely bites."""
    mutated = EXPECTED_MERGE_IF.replace("&&", "||")
    values = ["true", "false", ""]
    mismatch = False
    for skip in values:
        for merge in values:
            ctx = {
                "steps.guard.outputs.skip": skip,
                "steps.policy.outputs.merge": merge,
            }
            got = _eval_gha_bool_expr(mutated, ctx)
            expected = (skip != "true") and (merge == "true")
            if got is not expected:
                mismatch = True
    assert mismatch, "the && -> || mutation must diverge from the oracle"


def test_decision_outputs_are_step_outputs_not_github_env():
    """M-c: SKIP/MERGE must be step outputs, not $GITHUB_ENV writes that leak into
    the caller job's later steps."""
    doc = _load(ACTION_YML)
    guard = _step_by_id(doc, "guard")["run"]
    policy = _step_by_id(doc, "policy")["run"]
    assert 'skip=true' in guard and '"$GITHUB_OUTPUT"' in guard
    assert "GITHUB_ENV" not in guard and "GITHUB_ENV" not in policy
    assert '"$GITHUB_OUTPUT"' in policy


# -- executing the guard body -------------------------------------------------


def _guard_env(**over):
    base = {
        "ACTOR": "dependabot[bot]",
        "EVENT_NAME": "pull_request",
        "GH_TOKEN": "tok",
        "MERGE_METHOD": "squash",
        "TARGET_BRANCH": "dev",
    }
    base.update(over)
    return base


def test_guard_skips_a_non_dependabot_actor(tmp_path):
    guard = _step_by_id(_load(ACTION_YML), "guard")
    proc, out = _exec_run(guard, _guard_env(ACTOR="octocat"), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert out.get("skip") == "true"


def test_guard_skips_a_non_pull_request_event(tmp_path):
    guard = _step_by_id(_load(ACTION_YML), "guard")
    proc, out = _exec_run(guard, _guard_env(EVENT_NAME="schedule"), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert out.get("skip") == "true"


def test_guard_passes_a_dependabot_pull_request(tmp_path):
    guard = _step_by_id(_load(ACTION_YML), "guard")
    proc, out = _exec_run(guard, _guard_env(), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert out.get("skip") == "false"


def test_guard_fails_loudly_on_empty_token(tmp_path):
    guard = _step_by_id(_load(ACTION_YML), "guard")
    proc, out = _exec_run(guard, _guard_env(GH_TOKEN=""), tmp_path)
    assert proc.returncode == 1
    assert "github-token is required" in proc.stdout
    assert "skip" not in out


def test_guard_fails_loudly_on_bad_merge_method(tmp_path):
    guard = _step_by_id(_load(ACTION_YML), "guard")
    proc, _ = _exec_run(guard, _guard_env(MERGE_METHOD="fast-forward"), tmp_path)
    assert proc.returncode == 1
    assert "merge-method must be squash|merge|rebase" in proc.stdout


def test_guard_fails_loudly_on_empty_target_branch(tmp_path):
    guard = _step_by_id(_load(ACTION_YML), "guard")
    proc, _ = _exec_run(guard, _guard_env(TARGET_BRANCH="   "), tmp_path)
    assert proc.returncode == 1
    assert "target-branch is required" in proc.stdout


# -- end to end: guard -> policy -> gated merge with a stubbed gh -------------


def _stub_gh(tmp_path: Path) -> tuple[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "gh_calls.txt"
    gh = bin_dir / "gh"
    gh.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "' + str(calls) + '"\n')
    gh.chmod(0o755)
    calls.write_text("")
    return str(bin_dir), calls


# A well-formed OCI content digest (sha256: + 64 lowercase hex).
DIGEST = "sha256:" + "a" * 64


def _run_gate(tmp_path: Path, *, update_type: str, ecosystem: str,
              allowed_update_types: str | None = None,
              new_version: str = "1.2.3"):
    """Execute the real guard, policy and (gated) merge bodies for one simulated
    fetch-metadata result. Returns a dict describing what happened."""
    doc = _load(ACTION_YML)
    stub_dir, calls = _stub_gh(tmp_path)

    # 1) guard
    gproc, gout = _exec_run(_step_by_id(doc, "guard"), _guard_env(), tmp_path)
    assert gproc.returncode == 0, gproc.stderr
    skip = gout.get("skip", "true")

    # 2) policy (only if not skipped) — real script via $GITHUB_ACTION_PATH
    policy_env = {
        **DEFAULT_POLICY_ENV,
        "UPDATE_TYPE": update_type,
        "NEW_VERSION": new_version,
        "PACKAGE_ECOSYSTEM": ecosystem,
        "GITHUB_ACTION_PATH": str(ACTION_DIR),
    }
    if allowed_update_types is not None:
        policy_env["ALLOWED_UPDATE_TYPES"] = allowed_update_types
    pproc, pout = ({}, {})
    policy_rc = None
    merge_decision = None
    if skip != "true":
        pproc, pout = _exec_run(_step_by_id(doc, "policy"), policy_env, tmp_path)
        policy_rc = pproc.returncode
        merge_decision = pout.get("merge")

    # 3) merge, gated EXACTLY as the action's `if:` gates it.
    merge_if = _step_by_id(doc, "merge")["if"]
    assert "steps.guard.outputs.skip != 'true'" in merge_if
    assert "steps.policy.outputs.merge == 'true'" in merge_if
    armed = skip != "true" and merge_decision == "true"
    merged = False
    if armed:
        event = tmp_path / "event.json"
        event.write_text(json.dumps({"pull_request": {"html_url": "https://x/pr/1"}}))
        merge_env = {
            "GH_TOKEN": "tok",
            "MERGE_METHOD": "squash",
            "GITHUB_EVENT_PATH": str(event),
        }
        mproc, _ = _exec_run(
            _step_by_id(doc, "merge"), merge_env, tmp_path, stub_bin=stub_dir
        )
        assert mproc.returncode == 0, mproc.stderr
        merged = True
    calls_txt = calls.read_text()
    return {
        "policy_rc": policy_rc,
        "merge_decision": merge_decision,
        "merged": merged,
        "gh_calls": calls_txt,
    }


def test_gate_does_not_merge_a_docker_major(tmp_path):
    # The policy step exits 0 (a clean skip) but writes merge=false, so the merge
    # step's `if:` is never satisfied.
    r = _run_gate(tmp_path, update_type="version-update:semver-major", ecosystem="docker")
    assert r["policy_rc"] == 0, r
    assert r["merge_decision"] == "false"
    assert r["merged"] is False
    assert r["gh_calls"] == ""


def test_gate_does_not_merge_a_semver_major(tmp_path):
    r = _run_gate(tmp_path, update_type="version-update:semver-major", ecosystem="npm")
    assert r["policy_rc"] == 0, r
    assert r["merge_decision"] == "false"
    assert r["merged"] is False
    assert r["gh_calls"] == ""


def test_gate_merges_a_minor(tmp_path):
    r = _run_gate(tmp_path, update_type="version-update:semver-minor", ecosystem="npm")
    assert r["policy_rc"] == 0, r
    assert r["merge_decision"] == "true"
    assert r["merged"] is True
    assert "--auto" in r["gh_calls"] and "--squash" in r["gh_calls"]


def test_gate_merges_a_docker_digest(tmp_path):
    r = _run_gate(tmp_path, update_type="", ecosystem="docker", new_version=DIGEST)
    assert r["merged"] is True
    assert "--auto" in r["gh_calls"]


def test_gate_fails_loudly_on_docker_empty_update_type_without_a_digest(tmp_path):
    """An empty docker update-type whose new-version does NOT look like a digest
    must fail loudly (policy exits non-zero); the merge is never armed (no silent
    fail-open)."""
    r = _run_gate(tmp_path, update_type="", ecosystem="docker", new_version="1.2.3")
    assert r["policy_rc"] not in (0, 10), r  # loud failure, not merge/skip
    assert r["merge_decision"] is None
    assert r["merged"] is False and r["gh_calls"] == ""


def test_gate_does_not_merge_a_missing_or_unknown_update_type(tmp_path):
    """An empty/garbage update-type on a non-docker ecosystem is not a digest bump
    and is not in the allow-list: it must NOT merge (and never falls through to a
    default merge)."""
    r = _run_gate(tmp_path, update_type="", ecosystem="npm")
    assert r["merged"] is False and r["gh_calls"] == ""
    r = _run_gate(tmp_path, update_type="version-update:semver-weird", ecosystem="npm")
    assert r["merged"] is False and r["gh_calls"] == ""


def test_gate_fails_loudly_on_a_misspelled_allowed_update_type(tmp_path):
    """A typo'd allow-list token makes the policy step exit non-zero (misuse); the
    merge is never armed and there is no silent default-to-merge."""
    r = _run_gate(
        tmp_path,
        update_type="version-update:semver-patch",
        ecosystem="npm",
        allowed_update_types="semver-patch",
    )
    assert r["policy_rc"] not in (0, 10), r  # loud failure, not merge/skip
    assert r["merge_decision"] is None
    assert r["merged"] is False and r["gh_calls"] == ""
