"""Tests for the toolchain-consistency check (`toolchain_consistency.py`).

The check is verbatim ported code: it is imported straight from beside its
``action.yml`` so it runs standalone under any Python 3.12+ with only pytest,
exactly as it runs when the composite action invokes it with the runner's
``python3`` via ``$GITHUB_ACTION_PATH``. Fixtures are shaped like real
Dockerfiles — multi-stage with a stage alias, a digest-pinned
``lambda/python:3.14@sha256:...`` documented by a tag comment, a floating tag, a
``python:3.12-slim-bookworm`` base, and an ``ARG``-substituted ``FROM`` — but
with generic paths and values.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# toolchain_consistency.py lives beside the action.yml; import it from there.
_CHECK_DIR = Path(__file__).resolve().parents[1]
_SCRIPT = _CHECK_DIR / "toolchain_consistency.py"
sys.path.insert(0, str(_CHECK_DIR))

from toolchain_consistency import check, main  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "toolchain"


def _messages(name: str) -> list[str]:
    return [str(f) for f in check(FIXTURES / name)]


def _run(name: str) -> subprocess.CompletedProcess[str]:
    """Run the script exactly as the action would: as a subprocess with --root."""
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(FIXTURES / name)],
        capture_output=True,
        text=True,
    )


def test_clean_repo_exits_zero() -> None:
    proc = _run("clean")
    assert proc.returncode == 0, proc.stdout
    assert proc.stdout.strip() == ""


def test_clean_repo_has_no_findings() -> None:
    assert _messages("clean") == []


def test_version_mismatch_is_a_finding() -> None:
    messages = _messages("version_mismatch")
    assert any("3.12 differs from .python-version 3.14" in m for m in messages), messages


def test_stage_alias_is_not_checked_as_a_base() -> None:
    # `FROM deps AS app` resolves to the `deps` stage; it must not be reported as
    # its own base image (no second version/digest finding for `deps`/`app`).
    messages = _messages("version_mismatch")
    assert not any("app" in m or "FROM deps" in m for m in messages), messages


def test_digest_pinned_with_tag_comment_is_ok() -> None:
    # The clean fixture's runtime base is `lambda/python:3.14@sha256:...` with a
    # tag comment above it; it produces no finding.
    assert not any("sha256" in m for m in _messages("clean"))


def test_floating_tag_is_a_finding() -> None:
    messages = _messages("floating")
    assert any("not pinned by digest" in m for m in messages), messages


def test_arg_from_is_unresolvable() -> None:
    messages = _messages("arg_from")
    assert any("unresolvable" in m for m in messages), messages


def test_setup_python_with_python_version_is_a_finding() -> None:
    messages = _messages("setup_python")
    assert any("must not carry 'python-version:'" in m for m in messages), messages


def test_setup_python_without_version_file_is_a_finding() -> None:
    messages = _messages("setup_python")
    assert any("python-version-file: .python-version" in m for m in messages), messages


def test_setup_node_without_file_is_a_finding_when_nvmrc_present() -> None:
    messages = _messages("setup_node")
    assert any("node-version-file: .nvmrc" in m for m in messages), messages


def test_missing_python_version_is_a_finding() -> None:
    messages = _messages("missing_python_version")
    assert any(m.startswith(".python-version: missing") for m in messages), messages


def test_non_python_base_checked_only_for_digest() -> None:
    # A Node base yields a digest finding but never a Python-version finding.
    messages = _messages("node_base")
    assert any("not pinned by digest" in m for m in messages), messages
    assert not any(".python-version" in m for m in messages), messages


def test_docker_compose_is_ignored() -> None:
    # The compose file names `python:3.11`; it must not be scanned.
    messages = _messages("node_base")
    assert not any("docker-compose" in m for m in messages), messages
    assert not any("3.11 differs" in m for m in messages), messages


def test_every_finding_is_reported_not_just_the_first() -> None:
    messages = _messages("multi")
    # Floating tag, wrong version, unresolvable ARG, bad setup-python (x2), bad
    # setup-node — all present at once, none swallowed.
    assert any("not pinned by digest" in m for m in messages), messages
    assert any("3.12 differs from .python-version 3.14" in m for m in messages), messages
    assert any("unresolvable" in m for m in messages), messages
    assert any("must not carry 'python-version:'" in m for m in messages), messages
    assert any("node-version-file: .nvmrc" in m for m in messages), messages
    assert len(messages) >= 5, messages


def test_multi_exits_one() -> None:
    proc = _run("multi")
    assert proc.returncode == 1
    assert proc.stdout.strip() != ""


def test_main_returns_one_on_findings() -> None:
    assert main(["--root", str(FIXTURES / "floating")]) == 1


def test_main_returns_zero_when_clean() -> None:
    assert main(["--root", str(FIXTURES / "clean")]) == 0


@pytest.mark.parametrize(
    "name",
    ["clean", "version_mismatch", "floating", "arg_from", "multi"],
)
def test_script_is_executable_and_runs(name: str) -> None:
    proc = _run(name)
    assert proc.returncode in (0, 1)
