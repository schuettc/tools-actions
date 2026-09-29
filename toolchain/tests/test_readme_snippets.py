"""The action README's composition snippets must be real, not hand-waved.

The "Composing the pieces" section shows how a repo wires the ``toolchain``
composite action into its own PR workflow. Those snippets are the contract users
copy, so they must parse and lint exactly as GitHub would run them. For every
fenced ``yaml`` block that declares ``jobs:`` wiring the toolchain action we:

* rewrite the published pin ``schuettc/tools-actions/toolchain@vX.Y.Z`` to a
  local ``./toolchain`` reference and drop a copy of the real action there, so
  ``actionlint`` resolves the composite and checks each snippet's ``with:``
  inputs against the action's *declared* inputs (an unknown input, or a bad
  runner label, fails this suite);
* assert every caller job carries an explicit minimal ``permissions:`` block.

There is no hand-written test-only caller in place of the README's own snippets:
the snippets themselves are what runs. ``actionlint`` MUST run — it never skips;
when no binary is on PATH it falls back to ``uvx --from actionlint-py
actionlint`` and, failing that, the test fails (the ``toolchain`` CI job installs
the same pinned actionlint so this always runs there).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ACTION_DIR = Path(__file__).resolve().parents[1]
README = ACTION_DIR / "README.md"

_FENCE_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
_ACTION_REF = "schuettc/tools-actions/toolchain"


def _yaml_blocks(text: str) -> list[str]:
    return _FENCE_RE.findall(text)


def _uses_toolchain(step: dict) -> bool:
    uses = step.get("uses", "")
    return isinstance(uses, str) and (
        uses == "./toolchain" or uses == _ACTION_REF or uses.startswith(_ACTION_REF + "@")
    )


def _caller_jobs(text: str) -> list[tuple[str, dict, str]]:
    """Every (name, job, raw_block) declaring a job that wires the toolchain action."""
    jobs: list[tuple[str, dict, str]] = []
    for block in _yaml_blocks(text):
        doc = yaml.safe_load(block)
        if not isinstance(doc, dict) or "jobs" not in doc:
            continue
        for name, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            steps = job.get("steps") or []
            if any(isinstance(s, dict) and _uses_toolchain(s) for s in steps):
                jobs.append((name, job, block))
    return jobs


CALLER_JOBS = _caller_jobs(README.read_text())


def test_readme_has_caller_snippets() -> None:
    # Guard against the extraction silently matching nothing (which would make
    # every parametrized case vacuously pass).
    assert len(CALLER_JOBS) >= 2, (
        f"expected the README to wire the toolchain action, found {len(CALLER_JOBS)}"
    )


@pytest.mark.parametrize("case", CALLER_JOBS, ids=lambda c: c[0])
def test_readme_caller_job_declares_permissions(case: tuple[str, dict, str]) -> None:
    name, job, _block = case
    perms = job.get("permissions")
    assert perms, f"caller job {name!r} must declare an explicit minimal permissions: block"


def _actionlint() -> list[str] | None:
    binary = shutil.which("actionlint")
    if binary:
        return [binary]
    if shutil.which("uvx"):
        return ["uvx", "--from", "actionlint-py", "actionlint"]
    return None


@pytest.mark.parametrize("case", CALLER_JOBS, ids=lambda c: c[0])
def test_readme_caller_snippet_lints_against_real_action(
    case: tuple[str, dict, str], tmp_path: Path
) -> None:
    runner = _actionlint()
    if runner is None:
        pytest.fail(
            "actionlint is not available (no binary, no uvx): a test that "
            "validates README workflows must run, not skip. Install the pinned "
            "actionlint (see the 'toolchain' CI job) before running this suite."
        )
    _name, _job, block = case

    # Rewrite the published pin to a local ./toolchain ref so actionlint resolves
    # the composite and checks the snippet's `with:` inputs against its declared
    # inputs (a remote pin cannot be resolved for input checking).
    workflow = re.sub(rf"{re.escape(_ACTION_REF)}@v[\w.\-]+", "./toolchain", block)

    repo = tmp_path / "repo"
    (repo / "toolchain").mkdir(parents=True)
    shutil.copy(ACTION_DIR / "action.yml", repo / "toolchain" / "action.yml")
    shutil.copy(
        ACTION_DIR / "toolchain_consistency.py",
        repo / "toolchain" / "toolchain_consistency.py",
    )
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    # actionlint only knows hosted-runner labels up to its release; declare
    # ubuntu-26.04 (as this repo's own .github/actionlint.yaml does), else the
    # runs-on reads as an unknown label. The temp repo is not a git checkout, so
    # pass the config explicitly with -config-file.
    config_file = repo / ".github" / "actionlint.yaml"
    config_file.write_text("self-hosted-runner:\n  labels:\n    - ubuntu-26.04\n")
    caller = workflows / "caller.yml"
    caller.write_text("name: readme caller\non:\n  pull_request:\n" + workflow)

    result = subprocess.run(
        [*runner, "-config-file", str(config_file), str(caller)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"actionlint rejected a README snippet:\n{result.stdout}\n{result.stderr}"
    )
