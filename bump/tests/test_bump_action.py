"""Contract + lint tests for the ``bump`` composite action and its reusable workflow.

Ported and adapted from muda's ``test_bump_workflows.py``. tools-actions ships no
copier render layer, so instead of rendering jinja callers this suite asserts the
two shipped artifacts directly:

- ``bump/action.yml`` — the composite that carries ``bump_pin.py`` / ``bump_flow.py``
  and invokes them via ``$GITHUB_ACTION_PATH``: its input surface, native
  auto-merge (no ``sleep 60``), strict-bash run steps, exact action pins, and the
  absence of any project fact.
- ``.github/workflows/bump-pin.yml`` — the reusable workflow: its ``workflow_call``
  surface, per-package concurrency, the stall-on-``failure() || cancelled()`` step
  at the JOB level, and the ``bump`` composite pin equal to ``v$(cat VERSION)``.

``actionlint`` MUST run — it never skips. When no binary is on PATH it falls back
to ``uvx --from actionlint-py actionlint``; if neither is available the test
fails (the bump CI job installs the same pinned actionlint so this always runs).
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
REUSABLE = REPO_ROOT / ".github" / "workflows" / "bump-pin.yml"
README = ACTION_DIR / "README.md"
IMAGES_YML = REPO_ROOT / "images" / "action.yml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The reusable step-variable guard lives beside this test so other actions can
# adopt it; import it straight from there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stepvars import _env_writes, undefined_run_vars  # noqa: E402

# bump_pin.py lives beside the action; import the loaders so the README's toml
# examples are validated against the REAL config parser (I3).
sys.path.insert(0, str(ACTION_DIR))
from bump_pin import load_config, load_stage_globs  # noqa: E402

# Permission levels, weakest to strongest, for the C1 coverage assertion.
_PERM_RANK = {"none": 0, "read": 1, "write": 2}


def _fenced_blocks(md_text: str, lang: str) -> list[str]:
    """Every fenced ```<lang> block body in ``md_text`` (in document order)."""
    blocks: list[str] = []
    lines = md_text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == f"```{lang}":
            j = i + 1
            body: list[str] = []
            while j < len(lines) and lines[j].strip() != "```":
                body.append(lines[j])
                j += 1
            blocks.append("\n".join(body))
            i = j + 1
        else:
            i += 1
    return blocks


def _readme_caller_block() -> str:
    """The one fenced yaml block in the README that is a full reusable-workflow
    caller (has on:, jobs:, and calls bump-pin.yml)."""
    for block in _fenced_blocks(README.read_text(), "yaml"):
        if "jobs:" in block and "on:" in block and "bump-pin.yml" in block:
            return block
    raise AssertionError("no reusable-workflow caller block found in the README")

_VERSION_PIN = re.compile(r"@v\d+\.\d+\.\d+$")

# The exact composite inputs (and their defaults). ``None`` marks a required
# input with no default.
EXPECTED_INPUTS: dict[str, object] = {
    "package": None,
    "version": None,
    "check_consumer": None,
    "producer_release": None,
    "base_branch": None,
    "runner": None,
    "app_client_id": None,
    "config": "ci/bump/pins.toml",
    "private_index": "",
    "app_private_key": None,
    "index_role_arn": "",
}

# Generic-fixture / muda values that must NEVER appear in the project-fact-free
# action or reusable workflow (all facts arrive as inputs/secrets).
FORBIDDEN_FACTS = [
    "example-org",
    "111111111111",
    "222222222222",
    "shared-libs",
    "shared-py",
    "lib-a",
    "lib-b",
    "APP_CLIENT_ID",
    "APP_PRIVATE_KEY",
    "INDEX_ROLE_ARN",
    "RELEASE_APP",
    "muda",
]


def _version() -> str:
    return (REPO_ROOT / "VERSION").read_text().strip()


def _workflow_on(doc: dict) -> dict:
    # PyYAML parses the bare key `on:` as the boolean True.
    return doc.get("on", doc.get(True))


# --- composite action --------------------------------------------------------


def test_action_input_surface() -> None:
    assert ACTION_YML.exists(), f"missing {ACTION_YML}"
    doc = yaml.safe_load(ACTION_YML.read_text())
    assert doc["runs"]["using"] == "composite"

    inputs = doc["inputs"]
    assert set(inputs) == set(EXPECTED_INPUTS)
    for name, default in EXPECTED_INPUTS.items():
        spec = inputs[name]
        if default is None:
            assert spec.get("required") is True, f"{name} must be required"
            assert "default" not in spec, f"{name} must have no default"
        else:
            assert spec.get("default") == default, f"{name} default should be {default!r}"


def test_action_run_steps_are_strict_bash_and_use_action_path() -> None:
    doc = yaml.safe_load(ACTION_YML.read_text())
    steps = doc["runs"]["steps"]
    run_steps = [s for s in steps if "run" in s]
    assert run_steps, "expected run steps"
    for step in run_steps:
        assert step.get("shell") == "bash", f"{step.get('name')} needs shell: bash"
        body = step["run"].lstrip()
        # Strict bash. The supersede step deliberately opens `set -uo pipefail`
        # then `set +e` so it can CAPTURE bump_flow's exit 10 (newer PR open)
        # rather than die on it; every other step is `set -euo pipefail`.
        assert body.startswith(("set -euo pipefail", "set -uo pipefail")), (
            f"{step.get('name')} must start with a strict `set -[e]uo pipefail`"
        )

    # The scripts are invoked from beside the action via $GITHUB_ACTION_PATH,
    # never assumed on PATH or checked out elsewhere.
    blob = ACTION_YML.read_text()
    assert '"$GITHUB_ACTION_PATH/bump_pin.py"' in blob
    assert '"$GITHUB_ACTION_PATH/bump_flow.py"' in blob
    assert "ci/bump/bump_pin.py" not in blob  # no consumer-owned-script assumption
    assert "ci/bump/bump_flow.py" not in blob


def test_action_uses_native_auto_merge_not_sleep() -> None:
    raw = ACTION_YML.read_text()
    assert "gh pr merge" in raw and "--auto" in raw
    assert "sleep 60" not in raw
    assert "sleep 60" not in raw.replace(" ", "")


def test_action_third_party_pins_are_exact() -> None:
    doc = yaml.safe_load(ACTION_YML.read_text())
    used = [s["uses"] for s in doc["runs"]["steps"] if "uses" in s]
    assert used, "expected third-party action steps"
    for uses in used:
        assert _VERSION_PIN.search(uses), f"{uses} is not pinned to vX.Y.Z"


def test_action_scripts_exist_beside_it() -> None:
    assert (ACTION_DIR / "bump_pin.py").exists()
    assert (ACTION_DIR / "bump_flow.py").exists()


# --- reusable workflow -------------------------------------------------------


def test_reusable_workflow_call_surface() -> None:
    assert REUSABLE.exists(), f"missing {REUSABLE}"
    doc = yaml.safe_load(REUSABLE.read_text())
    call = _workflow_on(doc)["workflow_call"]
    assert set(call["inputs"]) == {
        "package",
        "version",
        "check_consumer",
        "producer_release",
        "base_branch",
        "app_client_id",
        "config",
        "private_index",
        "runner",
        "stall_label",
    }
    assert call["inputs"]["stall_label"]["required"] is True
    secrets = call["secrets"]
    assert set(secrets) == {"app_private_key", "index_role_arn"}
    assert secrets["app_private_key"]["required"] is True
    assert secrets["index_role_arn"].get("required", False) is False


def test_reusable_workflow_per_package_concurrency() -> None:
    doc = yaml.safe_load(REUSABLE.read_text())
    assert doc["concurrency"]["group"] == "bump-${{ inputs.package }}"
    assert doc["concurrency"]["cancel-in-progress"] is False


def test_reusable_workflow_stalls_on_failure_and_cancellation() -> None:
    """The stall step lives at the JOB level (a composite's post-failure steps do
    not run on a job-timeout cancellation) and fires on both conditions."""
    doc = yaml.safe_load(REUSABLE.read_text())
    steps = doc["jobs"]["bump"]["steps"]
    stall = [s for s in steps if "run" in s and "bump_flow.py" in s["run"] and "stall" in s["run"]]
    assert stall, "expected a stall run step"
    cond = stall[0]["if"]
    assert "failure()" in cond and "cancelled()" in cond


def test_reusable_workflow_pins_the_bump_composite_to_version() -> None:
    """The reusable workflow calls the bump composite at THIS repo's release tag;
    the contract tests require that pin to equal v$(cat VERSION)."""
    raw = REUSABLE.read_text()
    pins = re.findall(r"schuettc/tools-actions/bump@v[\w.\-]+", raw)
    assert pins, "reusable workflow must pin schuettc/tools-actions/bump@vX.Y.Z"
    for pin in pins:
        assert pin == f"schuettc/tools-actions/bump@v{_version()}", (
            f"{pin} does not match VERSION v{_version()}"
        )


def test_no_project_facts_leak() -> None:
    for artifact in (ACTION_YML, REUSABLE):
        raw = artifact.read_text()
        for needle in FORBIDDEN_FACTS:
            assert needle not in raw, f"project fact {needle!r} leaked into {artifact.name}"


# --- README pins -------------------------------------------------------------


def test_readme_pins_match_version() -> None:
    assert README.exists(), f"missing {README}"
    readme = README.read_text()
    pins = re.findall(r"schuettc/tools-actions/[\w.\-/]+@v[\w.\-]+", readme)
    assert pins, "README must pin schuettc/tools-actions/... @vX.Y.Z"
    for pin in pins:
        assert pin.endswith(f"@v{_version()}"), f"{pin} does not match VERSION v{_version()}"


# --- every run-step variable is defined (the BASE_BRANCH class of bug) --------


def test_action_has_no_undefined_run_step_variables() -> None:
    """Every ``$NAME``/``${NAME}`` a run step reads in the composite is defined by
    that step's ``env:``, an earlier ``$GITHUB_ENV`` write, a local assignment, a
    guarding ``${NAME:-}`` operator, or a GitHub default. A miss is a
    ``set -u`` ``unbound variable`` on every run \u2014 exactly how ``BASE_BRANCH``
    took the whole action down."""
    findings = undefined_run_vars(ACTION_YML)
    assert findings == [], f"undefined run-step variables in {ACTION_YML.name}: {findings}"


def test_reusable_workflow_has_no_undefined_run_step_variables() -> None:
    findings = undefined_run_vars(REUSABLE)
    assert findings == [], f"undefined run-step variables in {REUSABLE.name}: {findings}"


def test_guard_flags_a_dropped_env_var(tmp_path: Path) -> None:
    """The guard MUST fail on the pre-fix shape: strip ``BASE_BRANCH`` from the
    step ``env:`` and the guard names every step that then reads it unbound. This
    is the regression that shipped \u2014 the guard exists to make it impossible."""
    stripped = ACTION_YML.read_text().replace(
        "        BASE_BRANCH: ${{ inputs.base_branch }}\n", ""
    )
    assert stripped != ACTION_YML.read_text(), "expected BASE_BRANCH env lines to strip"
    broken = tmp_path / "action.yml"
    broken.write_text(stripped)
    findings = undefined_run_vars(broken)
    flagged = {name for _step, name in findings}
    assert "BASE_BRANCH" in flagged, f"guard did not catch the dropped BASE_BRANCH: {findings}"


def test_guard_runs_against_the_images_action() -> None:
    """The guard is reusable: run it against ``images/action.yml`` too. Any finding
    here is a real undefined-variable bug to report (this PR does not change
    images)."""
    if not IMAGES_YML.exists():
        pytest.skip("images/action.yml not present")
    findings = undefined_run_vars(IMAGES_YML)
    assert findings == [], f"undefined run-step variables in images/action.yml: {findings}"


# --- the three step-variable guard fixes -------------------------------------


def _composite(tmp_path: Path, *steps: dict) -> Path:
    """Write a minimal composite action.yml whose ``runs.steps`` are ``steps``."""
    p = tmp_path / "action.yml"
    p.write_text(
        yaml.safe_dump(
            {
                "name": "synthetic",
                "description": "synthetic guard fixture",
                "runs": {"using": "composite", "steps": list(steps)},
            },
            sort_keys=False,
        )
    )
    return p


def test_pre_fix_c1_shape_flags_base_branch() -> None:
    """The pre-fix C1 shape, captured as a FIXTURE FILE (not a ``git show <sha>``
    that skips when the commit is not in the checkout, so this always runs in CI):
    BASE_BRANCH is dropped from the bump step's ``env:`` and the guard must name it
    as an undefined read \u2014 the exact regression that shipped."""
    fixture = FIXTURES / "pre_fix_base_branch_action.yml"
    assert fixture.exists(), f"missing fixture {fixture}"
    flagged = {name for _step, name in undefined_run_vars(fixture)}
    assert "BASE_BRANCH" in flagged, (
        f"guard must catch the pre-fix C1 BASE_BRANCH shape: {flagged}"
    )


def test_2a_allowlist_excludes_gh_and_docker_env(tmp_path: Path) -> None:
    """2a: GH_TOKEN, GH_HOST and DOCKER_BUILDKIT are NOT GitHub default env vars.
    A bare read of any of them that the action does not define is a finding; the
    action must define it (env:, a local, or a $GITHUB_ENV write) itself."""
    step = {
        "name": "reads the ex-allowlisted names",
        "shell": "bash",
        "run": 'set -euo pipefail\necho "$GH_TOKEN $GH_HOST $DOCKER_BUILDKIT"\n',
    }
    flagged = {name for _s, name in undefined_run_vars(_composite(tmp_path, step))}
    assert flagged == {"GH_TOKEN", "GH_HOST", "DOCKER_BUILDKIT"}, flagged

    # Still fine once the step defines them properly.
    defined = dict(step, env={"GH_TOKEN": "x", "GH_HOST": "y", "DOCKER_BUILDKIT": "1"})
    assert undefined_run_vars(_composite(tmp_path, defined)) == []


def test_2b_guard_is_per_occurrence_not_script_wide(tmp_path: Path) -> None:
    """2b: ``${X:-}`` is safe only at that spot; it does not define X, so a later
    bare ``$X`` in the same script is still a finding. The two ASSIGNING forms
    ``${X:=v}`` / ``${X=v}`` are the exception \u2014 they set X."""
    leaky = {
        "name": "guard then bare",
        "shell": "bash",
        "run": 'set -euo pipefail\nif [ -z "${X:-}" ]; then exit 1; fi\necho "$X"\n',
    }
    flagged = {name for _s, name in undefined_run_vars(_composite(tmp_path, leaky))}
    assert flagged == {"X"}, flagged

    # A lone guarded occurrence (no later bare use) is clean.
    guarded_only = {
        "name": "guarded only",
        "shell": "bash",
        "run": 'set -euo pipefail\necho "${X:-fallback}"\n',
    }
    assert undefined_run_vars(_composite(tmp_path, guarded_only)) == []

    # The assigning expansion genuinely defines X for the later bare use.
    assigns = {
        "name": "assign then bare",
        "shell": "bash",
        "run": 'set -euo pipefail\n: "${X:=fallback}"\necho "$X"\n',
    }
    assert undefined_run_vars(_composite(tmp_path, assigns)) == []


def test_2c_env_write_needs_the_redirect_to_github_env(tmp_path: Path) -> None:
    """2c: a ``NAME=`` inside a heredoc/brace-group/print counts as a $GITHUB_ENV
    write only when THAT output is redirected to $GITHUB_ENV. A NAME= that lands
    on $GITHUB_STEP_SUMMARY does not become available to later steps just because
    the same script also touches $GITHUB_ENV elsewhere."""
    # A brace group redirected to $GITHUB_STEP_SUMMARY, in a script that also
    # writes $GITHUB_ENV: SUMMARY_ONLY must NOT be exported.
    to_summary = (
        "set -euo pipefail\n"
        'echo "REAL=1" >> "$GITHUB_ENV"\n'
        "{\n"
        '  echo "SUMMARY_ONLY=nope"\n'
        '} >> "$GITHUB_STEP_SUMMARY"\n'
    )
    assert _env_writes(to_summary) == {"REAL"}

    # Cross-step: a later step reading $SUMMARY_ONLY is a finding; $REAL is fine.
    producer = {"name": "producer", "shell": "bash", "run": to_summary}
    consumer = {
        "name": "consumer",
        "shell": "bash",
        "run": 'set -euo pipefail\necho "$REAL $SUMMARY_ONLY"\n',
    }
    flagged = {
        name for _s, name in undefined_run_vars(_composite(tmp_path, producer, consumer))
    }
    assert flagged == {"SUMMARY_ONLY"}, flagged

    # A heredoc / print redirected to $GITHUB_ENV DOES export its NAME=.
    heredoc = (
        "set -euo pipefail\n"
        "python3 - <<'PY' >> \"$GITHUB_ENV\"\n"
        'print("FROM_HEREDOC=1")\n'
        "PY\n"
    )
    assert _env_writes(heredoc) == {"FROM_HEREDOC"}


# --- actionlint (must run, never skip) ---------------------------------------


def _actionlint() -> list[str]:
    binary = shutil.which("actionlint")
    if binary:
        return [binary]
    if shutil.which("uvx"):
        return ["uvx", "--from", "actionlint-py", "actionlint"]
    pytest.fail(
        "actionlint is not available (no binary, no uvx): a test that validates "
        "workflows must run, not skip. Install the pinned actionlint (see the "
        "'bump' CI job) before running this suite."
    )


def _actionlint_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A tiny repo carrying the reusable workflow at a local path, so a caller can
    `uses: ./.github/workflows/bump-pin.yml` and actionlint resolves it. Returns
    ``(repo, actionlint_config_file)``."""
    repo = tmp_path / "repo"
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    shutil.copy(REUSABLE, workflows / "bump-pin.yml")
    config_file = repo / ".github" / "actionlint.yaml"
    config_file.write_text("self-hosted-runner:\n  labels:\n    - ubuntu-26.04\n")
    return repo, config_file


def _run_actionlint(repo: Path, config_file: Path, *targets: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            *_actionlint(),
            "-config-file",
            str(config_file),
            "-ignore",
            r'label ".+" is unknown',
            *[str(t) for t in targets],
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_actionlint_on_reusable_and_the_readme_caller(tmp_path: Path) -> None:
    """I3: the caller actionlinted here is the ONE lifted verbatim from the README
    (the remote `uses:` rewritten to the local path), not a hand-written stand-in.
    That is the gap C1 slipped through \u2014 the documented caller is now the tested
    caller, so a missing permissions block (or a bad with:/secrets:) fails CI."""
    repo, config_file = _actionlint_repo(tmp_path)
    caller_src = _readme_caller_block().replace(
        "schuettc/tools-actions/.github/workflows/bump-pin.yml@v" + _version(),
        "./.github/workflows/bump-pin.yml",
    )
    caller = repo / ".github" / "workflows" / "bump-lib.yml"
    caller.write_text(caller_src + "\n")
    result = _run_actionlint(repo, config_file, caller, repo / ".github" / "workflows" / "bump-pin.yml")
    assert result.returncode == 0, f"actionlint failed:\n{result.stdout}\n{result.stderr}"


# --- C1: the README caller's permissions cover the reusable workflow ---------


def test_readme_caller_permissions_cover_the_reusable() -> None:
    """A called reusable workflow can only NARROW the caller's token. The README
    caller must grant every permission key the reusable declares, at no weaker
    level, or every consumer copying it hits a startup failure (no job, no stall
    issue) \u2014 the C1 that shipped."""
    reusable = yaml.safe_load(REUSABLE.read_text())
    reusable_perms = reusable["permissions"]
    assert isinstance(reusable_perms, dict) and reusable_perms, "reusable must declare permissions"

    caller = yaml.safe_load(_readme_caller_block())
    job = caller["jobs"]["bump"]
    caller_perms = job.get("permissions") or caller.get("permissions") or {}
    assert caller_perms, "README caller has NO permissions block (C1 startup failure)"

    for key, level in reusable_perms.items():
        have = caller_perms.get(key, "none")
        assert _PERM_RANK[have] >= _PERM_RANK[level], (
            f"README caller grants {key}:{have}, weaker than the reusable's {key}:{level}"
        )


# --- I3: the README toml examples load through the REAL config parser ---------


def test_readme_toml_blocks_load_through_the_real_loaders(tmp_path: Path) -> None:
    """Every ```toml pins.toml example in the README parses cleanly through
    load_config AND load_stage_globs \u2014 a documented config that the shipped parser
    rejects is a lie the tests must catch (I3)."""
    blocks = [b for b in _fenced_blocks(README.read_text(), "toml") if "[[package]]" in b]
    assert blocks, "expected at least one pins.toml example in the README"
    for idx, block in enumerate(blocks):
        cfg = tmp_path / f"pins-{idx}.toml"
        cfg.write_text(block + "\n")
        packages = load_config(cfg)
        assert packages, f"README toml block #{idx} declared no packages"
        load_stage_globs(cfg)  # must not raise


# --- I2: a -latest runner / empty required input fails into the stall path ----


def _reject_step_script() -> str:
    doc = yaml.safe_load(ACTION_YML.read_text())
    for step in doc["runs"]["steps"]:
        if step.get("id") == "reject_inputs":
            return step["run"]
    raise AssertionError("no reject_inputs step in the composite action")


def test_reject_step_exists_and_is_first_validation() -> None:
    doc = yaml.safe_load(ACTION_YML.read_text())
    steps = doc["runs"]["steps"]
    ids = [s.get("id") for s in steps]
    assert "reject_inputs" in ids, "composite must reject *-latest / empty inputs"
    # It runs right after `expose` (which set BUMP_SCRIPTS so the job-level stall
    # step can still open an issue when this validation fails).
    assert ids.index("reject_inputs") == ids.index("expose") + 1


def _run_reject(runner: str, base_branch: str) -> subprocess.CompletedProcess:
    # The reject step reads RUNNER_LABEL/BASE_BRANCH from env; feed the real step
    # body to bash with those set, exactly as the composite runs it.
    body = _reject_step_script()
    return subprocess.run(
        ["bash", "-c", f"RUNNER_LABEL={runner!r} BASE_BRANCH={base_branch!r} bash -s"],
        input=body,
        capture_output=True,
        text=True,
    )


def test_reject_step_accepts_an_exact_runner() -> None:
    assert _run_reject("ubuntu-26.04", "dev").returncode == 0


def test_reject_step_fails_on_a_latest_runner() -> None:
    r = _run_reject("ubuntu-latest", "dev")
    assert r.returncode != 0
    assert "-latest" in (r.stdout + r.stderr)


def test_reject_step_fails_on_an_empty_runner() -> None:
    r = _run_reject("", "dev")
    assert r.returncode != 0
    assert "runner is empty" in (r.stdout + r.stderr)


def test_reject_step_fails_on_an_empty_base_branch() -> None:
    r = _run_reject("ubuntu-26.04", "")
    assert r.returncode != 0
    assert "base_branch is empty" in (r.stdout + r.stderr)


def test_reusable_passes_runner_to_the_composite() -> None:
    doc = yaml.safe_load(REUSABLE.read_text())
    step = doc["jobs"]["bump"]["steps"][0]
    assert step["with"]["runner"] == "${{ inputs.runner }}"


# --- I1: the stall step is resilient to a failure before checkout ------------


def test_stall_step_sparse_checks_out_the_config_and_passes_a_fallback_label() -> None:
    doc = yaml.safe_load(REUSABLE.read_text())
    steps = doc["jobs"]["bump"]["steps"]
    # A dedicated sparse checkout of the config precedes the stall run step, so a
    # failure BEFORE the composite's own checkout can still read pins.toml.
    checkout = [s for s in steps if s.get("id") == "stall_checkout"]
    assert checkout, "expected a stall_checkout step"
    assert "actions/checkout" in checkout[0]["uses"]
    assert checkout[0]["with"]["sparse-checkout"] == "${{ inputs.config }}"
    assert "failure()" in checkout[0]["if"] and "cancelled()" in checkout[0]["if"]

    stall = [s for s in steps if "run" in s and "bump_flow.py" in s["run"] and "stall" in s["run"]]
    assert stall, "expected a stall run step"
    run = stall[0]["run"]
    assert "--stall-label" in run, "stall step must pass a fallback label"
    # Its config points at the sparse-checkout path, not the bare consumer path.
    assert stall[0]["env"]["CONFIG"].startswith("__bump_stall_cfg/")


# --- M2: GITHUB_TOKEN permissions are minimal --------------------------------


def test_reusable_permissions_are_minimal() -> None:
    """Writes go through the App token, so GITHUB_TOKEN needs only issues:write
    and id-token:write; everything else is read (M2)."""
    doc = yaml.safe_load(REUSABLE.read_text())
    perms = doc["permissions"]
    assert perms["issues"] == "write"
    assert perms["id-token"] == "write"
    assert perms["contents"] == "read"
    assert perms["pull-requests"] == "read"
    assert perms["actions"] == "read"
    assert perms["checks"] == "read"


# --- M6: shellcheck the run scripts inside the composite + reusable -----------


def _bash_run_scripts(path: Path) -> list[tuple[str, str]]:
    doc = yaml.safe_load(path.read_text())
    runs = doc.get("runs")
    if isinstance(runs, dict) and "steps" in runs:
        steps = runs["steps"]
    else:
        steps = [s for job in (doc.get("jobs") or {}).values() for s in job.get("steps") or []]
    out: list[tuple[str, str]] = []
    for s in steps:
        if isinstance(s, dict) and "run" in s and s.get("shell", "bash") == "bash":
            out.append((s.get("name", "<unnamed>"), s["run"]))
    return out


def _shellcheck() -> list[str]:
    binary = shutil.which("shellcheck")
    if binary:
        return [binary]
    pytest.fail(
        "shellcheck is not on PATH: a test that lints shell must run, not skip. "
        "Install the pinned shellcheck (see the 'bump' CI job) before running this suite."
    )


def test_composite_and_reusable_run_scripts_are_shellcheck_clean(tmp_path: Path) -> None:
    runner = _shellcheck()
    gha = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
    failures: list[str] = []
    for path in (ACTION_YML, REUSABLE):
        for name, body in _bash_run_scripts(path):
            # Neutralise GitHub `${{ ... }}` expressions (not shell) to a token so
            # shellcheck sees valid bash.
            script = "#!/usr/bin/env bash\n" + gha.sub("GHA_EXPR", body)
            f = tmp_path / "s.sh"
            f.write_text(script)
            result = subprocess.run([*runner, str(f)], capture_output=True, text=True)
            if result.returncode != 0:
                failures.append(f"{path.name} :: {name}\n{result.stdout}")
    assert not failures, "shellcheck findings in run scripts:\n" + "\n".join(failures)


# --- I4: the step-variable guard's new powers --------------------------------


def test_guard_scans_env_context_in_run_if_and_with(tmp_path: Path) -> None:
    """`${{ env.X }}` in run:, if: and with: must resolve to a defined env name.
    An undefined env.X silently becomes '' at runtime \u2014 a finding here."""
    producer = {
        "name": "writes DEFINED to GITHUB_ENV",
        "shell": "bash",
        "run": 'set -euo pipefail\necho "DEFINED=1" >> "$GITHUB_ENV"\n',
    }
    reads_if = {
        "name": "reads env in if",
        "if": "${{ env.DEFINED == '1' && env.UNDEFINED_A != 'x' }}",
        "shell": "bash",
        "run": "set -euo pipefail\ntrue\n",
    }
    reads_with = {
        "name": "reads env in with",
        "uses": "some/action@v1",
        "with": {"region": "${{ env.UNDEFINED_B }}", "ok": "${{ env.DEFINED }}"},
    }
    reads_run = {
        "name": "reads env in run expr",
        "shell": "bash",
        "run": "set -euo pipefail\necho '${{ env.UNDEFINED_C }}'\n",
    }
    flagged = {
        ref for _s, ref in undefined_run_vars(
            _composite(tmp_path, producer, reads_if, reads_with, reads_run)
        )
    }
    assert flagged == {"env.UNDEFINED_A", "env.UNDEFINED_B", "env.UNDEFINED_C"}, flagged


def test_guard_conditional_github_env_write_does_not_satisfy_a_bare_read(tmp_path: Path) -> None:
    """A $GITHUB_ENV write inside an if is conditional: a later BARE $X can be
    unbound under set -u, so it is a finding \u2014 unless the reader guards ${X:-}.
    (The same conditional write DOES satisfy an env.X read.)"""
    producer = {
        "name": "conditional write",
        "shell": "bash",
        "run": (
            "set -euo pipefail\n"
            'if [ -n "${MAYBE:-}" ]; then\n'
            '  echo "COND=1" >> "$GITHUB_ENV"\n'
            "fi\n"
        ),
    }
    bare_reader = {
        "name": "bare read of a conditional write",
        "shell": "bash",
        "run": 'set -euo pipefail\necho "$COND"\n',
    }
    guarded_reader = {
        "name": "guarded read of a conditional write",
        "shell": "bash",
        "run": 'set -euo pipefail\necho "${COND:-}"\n',
    }
    env_reader = {
        "name": "env.X read of a conditional write",
        "if": "${{ env.COND == '1' }}",
        "shell": "bash",
        "run": "set -euo pipefail\ntrue\n",
    }
    flagged = [
        (s, v)
        for s, v in undefined_run_vars(
            _composite(tmp_path, producer, bare_reader, guarded_reader, env_reader)
        )
    ]
    names = {v for _s, v in flagged}
    assert "COND" in names, f"bare read of a conditional write must be flagged: {flagged}"
    # The guarded read and the env.X read are NOT findings.
    assert not any(s == "guarded read of a conditional write" for s, _v in flagged)
    assert "env.COND" not in names


def test_guard_is_order_aware_read_before_assignment(tmp_path: Path) -> None:
    """A read before its assignment is a finding; the reverse order is clean."""
    before = {
        "name": "read before assign",
        "shell": "bash",
        "run": 'set -euo pipefail\necho "$X"\nX=1\n',
    }
    assert {v for _s, v in undefined_run_vars(_composite(tmp_path, before))} == {"X"}

    after = {
        "name": "assign before read",
        "shell": "bash",
        "run": 'set -euo pipefail\nX=1\necho "$X"\n',
    }
    assert undefined_run_vars(_composite(tmp_path, after)) == []


def test_guard_inline_prefix_defines_only_for_that_command(tmp_path: Path) -> None:
    """`X=1 cmd` defines X for that one command only; a later bare $X is a finding.
    `echo X=1` is NOT an assignment."""
    inline = {
        "name": "inline prefix then bare",
        "shell": "bash",
        "run": 'set -euo pipefail\nX=1 env >/dev/null\necho "$X"\n',
    }
    assert {v for _s, v in undefined_run_vars(_composite(tmp_path, inline))} == {"X"}

    echo_not_assign = {
        "name": "echo is not an assignment",
        "shell": "bash",
        "run": 'set -euo pipefail\necho X=1\necho "$X"\n',
    }
    assert {v for _s, v in undefined_run_vars(_composite(tmp_path, echo_not_assign))} == {"X"}


def test_guard_handles_length_and_indirect_expansions(tmp_path: Path) -> None:
    """`${#VAR}` and `${!VAR}` both READ VAR and both abort under set -u if unset."""
    step = {
        "name": "length and indirect",
        "shell": "bash",
        "run": 'set -euo pipefail\necho "${#LEN} ${!IND}"\n',
    }
    assert {v for _s, v in undefined_run_vars(_composite(tmp_path, step))} == {"LEN", "IND"}

    defined = dict(step, env={"LEN": "x", "IND": "y"})
    assert undefined_run_vars(_composite(tmp_path, defined)) == []


def test_guard_value_with_subshell_is_a_persistent_assignment(tmp_path: Path) -> None:
    """`X=$(cmd a b)` is ONE assignment (spaces inside `$(...)` don't split it),
    so a later bare $X is satisfied \u2014 not mis-read as an inline prefix."""
    step = {
        "name": "subshell value",
        "shell": "bash",
        "run": 'set -euo pipefail\nX=$(printf "%s" hi)\necho "$X"\n',
    }
    assert undefined_run_vars(_composite(tmp_path, step)) == []
