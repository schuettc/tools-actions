"""Self-tests for the shared step-variable guard (``testlib/stepvars.py``).

``stepvars.py`` is the ONE copy of the static guard that asserts every variable a
``run:`` step (or a ``${{ env.X }}`` expression) dereferences is defined before it
is read. It is shared test tooling adopted by several actions' suites; these are
its OWN tests, exercised against synthetic composite fixtures (``_composite``) and
one captured regression fixture, so the guard's behaviour is proven independently
of any single action.

Ported from ``bump``'s guard self-tests when the guard moved to ``testlib``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Import the ONE shared guard as a package module.
sys.path.insert(0, str(REPO_ROOT))
from testlib.stepvars import _env_writes, undefined_run_vars  # noqa: E402


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
    as an undefined read — the exact regression that shipped."""
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
    ``${X:=v}`` / ``${X=v}`` are the exception — they set X."""
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


def test_guard_scans_env_context_in_run_if_and_with(tmp_path: Path) -> None:
    """`${{ env.X }}` in run:, if: and with: must resolve to a defined env name.
    An undefined env.X silently becomes '' at runtime — a finding here."""
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
    unbound under set -u, so it is a finding — unless the reader guards ${X:-}.
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
    so a later bare $X is satisfied — not mis-read as an inline prefix."""
    step = {
        "name": "subshell value",
        "shell": "bash",
        "run": 'set -euo pipefail\nX=$(printf "%s" hi)\necho "$X"\n',
    }
    assert undefined_run_vars(_composite(tmp_path, step)) == []
