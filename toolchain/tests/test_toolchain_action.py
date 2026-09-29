"""Contract tests for the ``toolchain`` composite action.

The action runs this action's own ``toolchain_consistency.py`` (beside its
``action.yml``, invoked via ``$GITHUB_ACTION_PATH`` with the runner's own
``python3``) inside the calling job. It embeds no project facts (the repo's own
version files are the source of truth, read from the scanned tree), and every
``run`` step is a strict bash step.

Where ``actionlint`` is available (binary, else ``uvx --from actionlint-py
actionlint``) the suite also lints a generated caller workflow that *uses* the
action, so the composite is proven parseable in a real workflow context. A test
that validates workflows must not silently no-op: if neither is available the
actionlint check *fails* (the ``toolchain`` CI job installs the same pinned
actionlint so this always runs there).

It also adopts the reusable step-variable guard (copied from ``bump`` as
``stepvars.py``): every shell variable a ``run:`` step dereferences must be
defined — the ``BASE_BRANCH`` class of ``set -u`` ``unbound variable`` bug.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ACTION_DIR = Path(__file__).resolve().parents[1]
ACTION_YML = ACTION_DIR / "action.yml"
REPO_ROOT = ACTION_DIR.parents[0]

# The step-variable guard is shared test tooling (testlib/, not an action). Its
# own behavioural tests live in testlib/tests; here we just adopt it.
sys.path.insert(0, str(REPO_ROOT))
from testlib.stepvars import undefined_run_vars  # noqa: E402

# The exact set of inputs (and their defaults) the action exposes. ``None`` marks
# a required input with no default.
EXPECTED_INPUTS: dict[str, object] = {
    "root": ".",
}

_VERSION_PIN = re.compile(r"@v\d+\.\d+\.\d+$")


@pytest.fixture
def action_doc() -> dict:
    assert ACTION_YML.exists(), f"missing {ACTION_YML}"
    with ACTION_YML.open() as fh:
        return yaml.safe_load(fh)


def test_toolchain_action_contract(action_doc: dict) -> None:
    runs = action_doc["runs"]
    assert runs["using"] == "composite"

    # Inputs and defaults are exactly as specified.
    inputs = action_doc["inputs"]
    assert set(inputs) == set(EXPECTED_INPUTS)
    for name, default in EXPECTED_INPUTS.items():
        spec = inputs[name]
        if default is None:
            assert spec.get("required") is True, f"{name} must be required"
            assert "default" not in spec, f"{name} must have no default"
        else:
            assert spec.get("default") == default, f"{name} default should be {default!r}"

    steps = runs["steps"]

    # No project facts baked in: no AWS credentials, no npm package, no repo name.
    blob = yaml.safe_dump(action_doc)
    assert "configure-aws-credentials" not in blob

    # Every third-party action (if any) is pinned to an exact vX.Y.Z tag.
    for uses in [step["uses"] for step in steps if "uses" in step]:
        assert _VERSION_PIN.search(uses), f"{uses} is not pinned to vX.Y.Z"

    # Every run step is a strict bash step.
    run_steps = [step for step in steps if "run" in step]
    assert run_steps, "expected run steps"
    for step in run_steps:
        assert step.get("shell") == "bash", f"{step.get('name')} needs shell: bash"
        assert step["run"].lstrip().startswith("set -euo pipefail"), (
            f"{step.get('name')} must start with set -euo pipefail"
        )

    # toolchain_consistency.py is invoked from beside the action via
    # $GITHUB_ACTION_PATH, with the runner's python3, never assumed on PATH.
    run_script = _run_script(action_doc)
    assert '"$GITHUB_ACTION_PATH/toolchain_consistency.py"' in run_script
    assert "python3 " in run_script
    # The scan root is a caller input, not a hardcoded project fact.
    assert "$TOOLCHAIN_ROOT" in run_script


def test_readme_pins_match_version() -> None:
    # Every `schuettc/tools-actions/toolchain@vX.Y.Z` pin in the README must
    # equal `v` + the repo VERSION (mirrors images' snippet-pin test), so a
    # release bump can never leave a stale pin in the docs.
    root = ACTION_DIR.parents[0]
    version = (root / "VERSION").read_text().strip()
    readme = (ACTION_DIR / "README.md").read_text()
    pins = re.findall(r"schuettc/tools-actions/toolchain@v[\w.\-]+", readme)
    assert pins, "README must pin schuettc/tools-actions/toolchain@vX.Y.Z"
    for pin in pins:
        assert pin == f"schuettc/tools-actions/toolchain@v{version}", (
            f"{pin} does not match VERSION v{version}"
        )


def test_action_has_no_undefined_run_step_variables() -> None:
    """Every ``$NAME``/``${NAME}`` a run step reads is defined by that step's
    ``env:``, an earlier ``$GITHUB_ENV`` write, a local assignment, a guarding
    ``${NAME:-}`` operator, or a GitHub default. A miss is a ``set -u``
    ``unbound variable`` on every run (the ``BASE_BRANCH`` class of bug)."""
    findings = undefined_run_vars(ACTION_YML)
    assert findings == [], f"undefined run-step variables in {ACTION_YML.name}: {findings}"


def test_action_run_scripts_pass_shellcheck(action_doc: dict, tmp_path: Path) -> None:
    """Every ``run:`` script embedded in action.yml is a bash script GitHub runs,
    but it is not a ``*.sh`` file so the repo-wide shellcheck job never sees it.
    Extract each and shellcheck it here (with the step's env names declared so a
    genuine finding is not masked). shellcheck MUST run \u2014 it never skips; if no
    binary is on PATH the test fails (the CI job installs the pinned shellcheck).
    """
    binary = shutil.which("shellcheck")
    if binary is None:
        pytest.fail(
            "shellcheck is not available: a test that lints action.yml run "
            "scripts must run, not skip. Install the pinned shellcheck (see the "
            "'toolchain' CI job) before running this suite."
        )
    run_steps = [s for s in action_doc["runs"]["steps"] if "run" in s]
    assert run_steps, "expected run steps to shellcheck"
    for i, step in enumerate(run_steps):
        # Declare the step's env: names (and GITHUB_ACTION_PATH) so their bare use
        # is not a spurious SC2154; the stepvars guard already proves they are set.
        env_names = list((step.get("env") or {}).keys()) + ["GITHUB_ACTION_PATH"]
        preamble = "".join(f'{name}=""\n' for name in env_names)
        script = tmp_path / f"step_{i}.sh"
        script.write_text("#!/usr/bin/env bash\n" + preamble + step["run"])
        result = subprocess.run(
            [binary, str(script)], capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"shellcheck failed on run step {step.get('name')!r}:\n"
            f"{result.stdout}\n{result.stderr}"
        )


def test_run_script_rejects_empty_root(action_doc: dict, tmp_path: Path) -> None:
    """The real run script must fail loudly on an empty ``root``: ``root`` has a
    default of ``"."``, so an empty ``TOOLCHAIN_ROOT`` can only be a caller
    explicitly passing ``""`` \u2014 never a silent scan of the wrong tree. Extract
    the actual script GitHub runs and execute it with ``TOOLCHAIN_ROOT=""``,
    asserting exit code 1 and the exact error, so the guard is proven, not just
    read. (The guard uses ``[ -z ... ]``, which does not reject a whitespace-only
    value, so that input is intentionally *not* asserted to be rejected.)
    """
    run_script = _run_script(action_doc)
    script = tmp_path / "run.sh"
    script.write_text("#!/usr/bin/env bash\n" + run_script)
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={**os.environ, "TOOLCHAIN_ROOT": ""},
    )
    assert result.returncode == 1, (
        f"empty root must exit 1, got {result.returncode}:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert (
        "toolchain action: 'root' must not be empty (default is '.')"
        in result.stderr
    ), f"missing empty-root error on stderr:\n{result.stderr}"


def _run_script(action_doc: dict) -> str:
    for step in action_doc["runs"]["steps"]:
        if step.get("id") == "run":
            return step["run"]
    raise AssertionError("no step with id: run")


def _actionlint() -> list[str] | None:
    binary = shutil.which("actionlint")
    if binary:
        return [binary]
    if shutil.which("uvx"):
        return ["uvx", "--from", "actionlint-py", "actionlint"]
    return None


def test_actionlint_on_caller_workflow(tmp_path: Path) -> None:
    runner = _actionlint()
    if runner is None:
        pytest.fail(
            "actionlint is not available (no binary, no uvx): a test that "
            "validates workflows must run, not skip. Install the pinned "
            "actionlint (see the 'toolchain' CI job) before running this suite."
        )

    # A tiny repo containing a copy of the action and a caller workflow that USES
    # it via a local `./toolchain` reference, so actionlint resolves the composite
    # and checks the `with:` inputs against its declared inputs.
    repo = tmp_path / "repo"
    (repo / "toolchain").mkdir(parents=True)
    shutil.copy(ACTION_YML, repo / "toolchain" / "action.yml")
    shutil.copy(ACTION_DIR / "toolchain_consistency.py", repo / "toolchain" / "toolchain_consistency.py")
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    # actionlint only knows hosted-runner labels up to its release, so declare
    # ubuntu-26.04 (as this repo's own .github/actionlint.yaml does) in the
    # generated repo, else the caller's runs-on reads as an unknown label. The
    # temp repo is not a git checkout, so pass the config explicitly with
    # -config-file (auto-discovery only fires inside a git project).
    config_file = repo / ".github" / "actionlint.yaml"
    config_file.write_text("self-hosted-runner:\n  labels:\n    - ubuntu-26.04\n")
    caller = workflows / "caller.yml"
    caller.write_text(
        "\n".join(
            [
                "name: toolchain caller",
                "on:",
                "  pull_request:",
                "jobs:",
                "  toolchain:",
                "    runs-on: ubuntu-26.04",
                "    steps:",
                "      - uses: actions/checkout@v7.0.1",
                "      - uses: ./toolchain",
                "",
            ]
        )
    )

    result = subprocess.run(
        [*runner, "-config-file", str(config_file), str(caller)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"actionlint failed:\n{result.stdout}\n{result.stderr}"
