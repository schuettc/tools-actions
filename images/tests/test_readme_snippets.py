"""The action README's composition snippets must run against the REAL CLI.

The "Composing the pieces" section shows how a repo wires the ``images``
composite action into its own PR/deploy workflows. Those snippets are the
contract users copy, so they must not drift from ``images.py``'s argparse: a
snippet the CLI would reject (a bad ``--cache`` choice, a missing required
``--account``/``--registry``, an unknown flag) must fail this suite.

For every fenced ``yaml`` block in the README we find each step that uses the
images composite action and reconstruct the exact argv the composite action
would hand ``images.py``:

    images.py <command> --config <cfg> --cdk-out <out> <args...>

then parse it with ``images.py``'s own ``_build_parser()``. ``${{ … }}``
expressions (which GitHub, not the shell, resolves) are replaced by a
placeholder token before word-splitting. We also assert no snippet passes
``--github-output`` (the action wires that for ``promote`` itself; callers must
never pass it).
"""

from __future__ import annotations

import contextlib
import io
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

_ACTION_DIR = Path(__file__).resolve().parents[1]
README = _ACTION_DIR / "README.md"

# images.py lives beside the README; import it straight from there.
sys.path.insert(0, str(_ACTION_DIR))

import images  # noqa: E402

# A GitHub Actions expression is resolved by GitHub before the value ever reaches
# the shell, so for CLI-shape validation it stands in for an arbitrary literal.
_EXPR_RE = re.compile(r"\$\{\{.*?\}\}")
_PLACEHOLDER = "EXPR_PLACEHOLDER"

_FENCE_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
# The README composition snippets reference the action by its published or local
# path; either form marks a step that wires the images action.
_ACTION_REFS = ("./images", "schuettc/tools-actions/images")


def _yaml_blocks(text: str) -> list[str]:
    return _FENCE_RE.findall(text)


def _is_images_step(step: dict) -> bool:
    uses = step.get("uses", "")
    return isinstance(uses, str) and any(
        uses == ref or uses.startswith(ref + "@") for ref in _ACTION_REFS
    )


def _image_steps(text: str) -> list[dict]:
    """Every step in the README that uses the images composite action."""
    steps: list[dict] = []
    for block in _yaml_blocks(text):
        doc = yaml.safe_load(block)
        if not isinstance(doc, dict):
            continue
        for job in (doc.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if isinstance(step, dict) and _is_images_step(step):
                    steps.append(step)
    return steps


def _argv_for(step: dict) -> list[str]:
    """Reconstruct the argv the composite action passes to images.py."""
    with_ = step.get("with") or {}
    command = str(with_["command"])
    raw_args = str(with_.get("args", "") or "")
    args = _EXPR_RE.sub(_PLACEHOLDER, raw_args)
    return [command, "--config", "x", "--cdk-out", "y", *shlex.split(args)]


README_STEPS = _image_steps(README.read_text())


def test_readme_has_image_action_snippets() -> None:
    # Guard against the extraction silently matching nothing (which would make
    # every parametrized case vacuously pass).
    assert len(README_STEPS) >= 5, (
        f"expected the README to wire the images action several times, found {len(README_STEPS)}"
    )


@pytest.mark.parametrize("step", README_STEPS, ids=lambda s: (s.get("with") or {}).get("command"))
def test_readme_image_snippet_parses_against_real_cli(step: dict) -> None:
    argv = _argv_for(step)
    parser = images._build_parser()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits (code 2) on any parse error
        pytest.fail(
            f"README images snippet rejected by images.py CLI (exit {exc.code}):\n"
            f"  argv: {argv}\n  argparse said: {stderr.getvalue().strip()}"
        )


@pytest.mark.parametrize("step", README_STEPS, ids=lambda s: (s.get("with") or {}).get("command"))
def test_readme_image_snippet_never_passes_github_output(step: dict) -> None:
    raw_args = str((step.get("with") or {}).get("args", "") or "")
    assert "--github-output" not in raw_args, (
        "README snippet passes --github-output via args; the action wires it for "
        "promote itself and rejects a caller-supplied one"
    )
