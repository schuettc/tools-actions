"""Contract tests for the ``images`` composite action.

The action runs this action's own ``images.py`` (beside its ``action.yml``,
invoked via ``$GITHUB_ACTION_PATH``) inside the calling job. It owns no AWS
credentials (the calling job does), reads the ECR ``region`` from the images
config (a project fact, never an input default), pins every third-party action
to an exact ``vX.Y.Z`` tag, and every ``run`` step is a strict bash step.

Where ``actionlint`` is available (binary, else ``uvx --from actionlint-py
actionlint``) the suite also lints a generated caller workflow that *uses* the
action, so the composite is proven parseable in a real workflow context. A test
that validates workflows must not silently no-op: if neither is available the
actionlint check *fails* (the ``images`` CI job installs the same pinned
actionlint so this always runs there).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ACTION_DIR = Path(__file__).resolve().parents[1]
ACTION_YML = ACTION_DIR / "action.yml"

# The exact set of inputs (and their defaults) the action exposes. ``None`` marks
# a required input with no default.
EXPECTED_INPUTS: dict[str, object] = {
    "command": None,
    "cdk-out": None,
    "config": "ci/images/images.toml",
    "args": "",
    "ecr-login-accounts": "",
    "setup-buildx": "false",
}

_VERSION_PIN = re.compile(r"@v\d+\.\d+\.\d+$")


@pytest.fixture
def action_doc() -> dict:
    assert ACTION_YML.exists(), f"missing {ACTION_YML}"
    with ACTION_YML.open() as fh:
        return yaml.safe_load(fh)


def test_images_action_contract(action_doc: dict) -> None:
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

    # No AWS credentials step: the calling job owns roles.
    blob = yaml.safe_dump(action_doc)
    assert "configure-aws-credentials" not in blob

    # Every third-party action is pinned to an exact vX.Y.Z tag.
    used = [step["uses"] for step in steps if "uses" in step]
    assert used, "expected at least the buildx setup action"
    for uses in used:
        assert _VERSION_PIN.search(uses), f"{uses} is not pinned to vX.Y.Z"

    # Every run step is a strict bash step.
    run_steps = [step for step in steps if "run" in step]
    assert run_steps, "expected run steps"
    for step in run_steps:
        assert step.get("shell") == "bash", f"{step.get('name')} needs shell: bash"
        assert step["run"].lstrip().startswith("set -euo pipefail"), (
            f"{step.get('name')} must start with set -euo pipefail"
        )

    # images.py is invoked from beside the action via $GITHUB_ACTION_PATH, never
    # assumed to be on PATH or checked out elsewhere.
    run_script = _run_images_script(action_doc)
    assert '"$GITHUB_ACTION_PATH/images.py"' in run_script

    # The action wires promote's --github-output itself (callers never pass it):
    # it appends it only for `promote`, and rejects a caller-supplied one.
    assert 'if [ "$IMAGES_COMMAND" = "promote" ]' in run_script
    assert "--github-output" in run_script
    assert '"$GITHUB_OUTPUT"' in run_script
    # The callers' args must never carry --github-output (loud rejection).
    assert "exit 1" in run_script


def test_readme_pins_match_version() -> None:
    # M5: every `schuettc/tools-actions/images@vX.Y.Z` pin in the README must
    # equal `v` + the repo VERSION (mirrors fork-sync's snippet-pin test), so a
    # release bump can never leave a stale pin in the docs.
    root = ACTION_DIR.parents[0]
    version = (root / "VERSION").read_text().strip()
    readme = (ACTION_DIR / "README.md").read_text()
    pins = re.findall(r"schuettc/tools-actions/images@v[\w.\-]+", readme)
    assert pins, "README must pin schuettc/tools-actions/images@vX.Y.Z"
    for pin in pins:
        assert pin == f"schuettc/tools-actions/images@v{version}", (
            f"{pin} does not match VERSION v{version}"
        )


def _run_images_script(action_doc: dict) -> str:
    """The bash body of the ``Run images.py`` step."""
    for step in action_doc["runs"]["steps"]:
        if step.get("id") == "run":
            return step["run"]
    raise AssertionError("no step with id: run")


# A stub images.py: records its argv and, when handed --github-output <path>,
# appends the dev-digests line to that path (mimicking promote's real output).
_STUB_IMAGES_PY = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import pathlib
    import sys

    argv = sys.argv[1:]
    pathlib.Path("argv.txt").write_text("\\n".join(argv))
    if "--github-output" in argv:
        out = argv[argv.index("--github-output") + 1]
        with open(out, "a") as fh:
            fh.write('dev-digests={"h":"sha256:x"}\\n')
    """
)


def _run_step(
    script: str, workdir: Path, *, command: str, args: str = ""
) -> subprocess.CompletedProcess:
    workdir.mkdir(parents=True, exist_ok=True)
    # The action invokes "$GITHUB_ACTION_PATH/images.py": place the stub there.
    action_path = workdir / "action"
    action_path.mkdir(parents=True, exist_ok=True)
    (action_path / "images.py").write_text(_STUB_IMAGES_PY)
    (workdir / "ci" / "images").mkdir(parents=True, exist_ok=True)
    config = workdir / "ci" / "images" / "images.toml"
    config.write_text('region = "us-east-1"\n')
    github_output = workdir / "gh_output"
    github_output.write_text("")
    env = {
        **os.environ,
        "GITHUB_ACTION_PATH": str(action_path),
        "IMAGES_COMMAND": command,
        "IMAGES_CDK_OUT": "cdk.out",
        "IMAGES_CONFIG": str(config),
        "IMAGES_ARGS": args,
        "GITHUB_OUTPUT": str(github_output),
    }
    return subprocess.run(
        ["bash", "-c", script],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
    )


def test_run_step_wires_promote_output(action_doc: dict, tmp_path: Path) -> None:
    script = _run_images_script(action_doc)

    # promote: the action appends --github-output, so the stub writes dev-digests
    # to the temp GITHUB_OUTPUT file.
    promote_dir = tmp_path / "promote"
    result = _run_step(script, promote_dir, command="promote")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert 'dev-digests={"h":"sha256:x"}' in (promote_dir / "gh_output").read_text()
    assert "--github-output" in (promote_dir / "argv.txt").read_text()

    # build: no --github-output is passed and nothing is written to GITHUB_OUTPUT.
    build_dir = tmp_path / "build"
    result = _run_step(script, build_dir, command="build", args="--mode push --registry r")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (build_dir / "gh_output").read_text() == ""
    assert "--github-output" not in (build_dir / "argv.txt").read_text()


def test_run_step_passes_config_and_cdk_out(action_doc: dict, tmp_path: Path) -> None:
    script = _run_images_script(action_doc)
    d = tmp_path / "list"
    result = _run_step(script, d, command="list")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    argv = (d / "argv.txt").read_text().splitlines()
    assert argv[0] == "list"
    assert "--config" in argv
    assert "--cdk-out" in argv
    assert argv[argv.index("--cdk-out") + 1] == "cdk.out"


def test_run_step_rejects_caller_github_output(action_doc: dict, tmp_path: Path) -> None:
    script = _run_images_script(action_doc)
    result = _run_step(
        script,
        tmp_path / "reject",
        command="promote",
        args="--github-output /tmp/whatever",
    )
    assert result.returncode != 0
    assert "do not pass --github-output" in result.stderr


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
            "actionlint (see the 'images' CI job) before running this suite."
        )

    # A tiny repo containing a copy of the action and a caller workflow that USES
    # it via a local `./images` reference, so actionlint resolves the composite
    # and checks the `with:` inputs against its declared inputs.
    repo = tmp_path / "repo"
    (repo / "images").mkdir(parents=True)
    shutil.copy(ACTION_YML, repo / "images" / "action.yml")
    shutil.copy(ACTION_DIR / "images.py", repo / "images" / "images.py")
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
                "name: images caller",
                "on:",
                "  workflow_dispatch:",
                "jobs:",
                "  build:",
                "    runs-on: ubuntu-26.04",
                "    steps:",
                "      - uses: actions/checkout@v7.0.1",
                "      - uses: ./images",
                "        with:",
                "          command: build",
                "          cdk-out: cdk.out",
                "          setup-buildx: 'true'",
                "          args: --mode push --cache readwrite --registry r",
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
