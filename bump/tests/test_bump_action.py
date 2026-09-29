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

# The reusable step-variable guard lives beside this test so other actions can
# adopt it; import it straight from there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stepvars import _env_writes, undefined_run_vars  # noqa: E402

_VERSION_PIN = re.compile(r"@v\d+\.\d+\.\d+$")

# The exact composite inputs (and their defaults). ``None`` marks a required
# input with no default.
EXPECTED_INPUTS: dict[str, object] = {
    "package": None,
    "version": None,
    "check_consumer": None,
    "producer_release": None,
    "base_branch": None,
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
    }
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
    stall = [s for s in steps if "stall" in (s.get("name", "").lower())]
    assert stall, "expected a stall step"
    cond = stall[0]["if"]
    assert "failure()" in cond and "cancelled()" in cond
    assert "bump_flow.py" in stall[0]["run"] and "stall" in stall[0]["run"]


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


def test_pre_fix_c1_shape_flags_base_branch(tmp_path: Path) -> None:
    """The exact shape that shipped: ``git show 2463203:bump/action.yml`` (the
    commit BEFORE BASE_BRANCH was redistributed onto the step ``env:`` blocks)
    must still be caught \u2014 the guard names BASE_BRANCH as an undefined read."""
    raw = subprocess.run(
        ["git", "show", "2463203:bump/action.yml"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if raw.returncode != 0:
        pytest.skip("commit 2463203 not reachable in this checkout")
    fixture = tmp_path / "c1-action.yml"
    fixture.write_text(raw.stdout)
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


def test_actionlint_on_reusable_and_a_caller(tmp_path: Path) -> None:
    runner = _actionlint()

    # A tiny repo with the reusable workflow and a caller that USES it via a local
    # path, so actionlint resolves the reusable and checks the `with:`/`secrets:`
    # against its declared workflow_call surface.
    repo = tmp_path / "repo"
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    shutil.copy(REUSABLE, workflows / "bump-pin.yml")

    # actionlint only knows hosted-runner labels up to its release; declare
    # ubuntu-26.04 so a pinned image is not read as an unknown label.
    config_file = repo / ".github" / "actionlint.yaml"
    config_file.write_text("self-hosted-runner:\n  labels:\n    - ubuntu-26.04\n")

    caller = workflows / "bump-lib.yml"
    caller.write_text(
        "\n".join(
            [
                "name: Bump lib",
                "on:",
                "  repository_dispatch:",
                '    types: ["lib-published"]',
                "  workflow_dispatch:",
                "    inputs:",
                "      package:",
                "        required: true",
                "        type: string",
                "      version:",
                "        required: true",
                "        type: string",
                "jobs:",
                "  bump:",
                "    uses: ./.github/workflows/bump-pin.yml",
                "    with:",
                "      package: ${{ github.event_name == 'workflow_dispatch' "
                "&& inputs.package || github.event.client_payload.package }}",
                "      version: ${{ github.event_name == 'workflow_dispatch' "
                "&& inputs.version || github.event.client_payload.version }}",
                "      check_consumer: false",
                '      producer_release: "lib release"',
                "      base_branch: main",
                "      app_client_id: ${{ vars.RELEASE_APP_CLIENT_ID }}",
                "      runner: ubuntu-26.04",
                '      private_index: ""',
                "    secrets:",
                "      app_private_key: ${{ secrets.RELEASE_APP_PRIVATE_KEY }}",
                "",
            ]
        )
    )

    result = subprocess.run(
        [
            *runner,
            "-config-file",
            str(config_file),
            "-ignore",
            r'label ".+" is unknown',
            str(caller),
            str(workflows / "bump-pin.yml"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"actionlint failed:\n{result.stdout}\n{result.stderr}"
