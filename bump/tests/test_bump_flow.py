"""Tests for the pin-bump chain decision logic (``bump_flow.py``).

Ported verbatim from muda's ``tests/kits/delivery/test_bump_flow.py``: the 13
decision cases plus ``test_stall_label_from_config`` (the label is a project
answer read from ``pins.toml``, not a fact baked in code). The module imports
``bump_flow.py`` straight from beside this test (the action directory), so it
runs standalone under any Python 3.12+ with only pytest.

Every case here is offline: the single `_run` subprocess site is monkeypatched to
a fake that returns scripted `gh` output and records the argv it was asked to
run, and `await`'s clock/sleep are injected. Nothing in this file may reach
GitHub — the decisions are pinned by a test, not discovered mid-release.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# bump_flow.py lives beside the action; import it straight from there.
_BUMP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BUMP_DIR))

import bump_flow  # noqa: E402

REPO = "example-org/example-repo"
PKG = "lib-a"


class FakeRun:
    """Records every argv and answers from a per-test responder.

    A responder returning ``None`` means "this command was not scripted" and is a
    test bug, so it fails loudly rather than silently returning "".
    """

    def __init__(self, responder: Callable[[list[str]], str | None]) -> None:
        self._responder = responder
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        out = self._responder(args)
        if out is None:
            raise AssertionError(f"unscripted command: {args}")
        return out

    def commands_containing(self, *needles: str) -> list[list[str]]:
        return [c for c in self.calls if all(n in c for n in needles)]


def _install(monkeypatch: Any, fake: FakeRun) -> None:
    monkeypatch.setattr(bump_flow, "_run", fake)
    monkeypatch.setattr(bump_flow, "_sleep", lambda _s: None)
    monkeypatch.setattr(bump_flow, "_now", lambda: 0.0)


# --- version comparison ------------------------------------------------------


def test_versions_compare_as_integer_tuples_not_strings() -> None:
    """The bug a lexical compare hides: `1.9.0 < 1.10.0` is only true if the
    components are compared as integers. A string compare puts `1.10.0` first."""
    assert bump_flow._parse_version("1.9.0") < bump_flow._parse_version("1.10.0")


# --- supersede ---------------------------------------------------------------


def _pr_list_json(prs: list[dict[str, Any]]) -> str:
    return json.dumps(prs)


def test_supersede_closes_an_older_pr_and_ignores_other_packages(
    monkeypatch: Any, capsys: Any
) -> None:
    prs = [
        {
            "number": 700,
            "headRefName": f"chore/bump-{PKG}-1.40.0",
            "url": "https://example/pr/700",
        },
        # A different package's bump branch — must be left completely alone.
        {
            "number": 701,
            "headRefName": "chore/bump-lib-b-1.40.0",
            "url": "https://example/pr/701",
        },
        # Unrelated branch.
        {
            "number": 702,
            "headRefName": "dependabot/npm/foo",
            "url": "https://example/pr/702",
        },
    ]

    def respond(args: list[str]) -> str | None:
        if "list" in args:
            return _pr_list_json(prs)
        if "close" in args:
            return ""
        return None

    fake = FakeRun(respond)
    _install(monkeypatch, fake)

    code = bump_flow.main(["supersede", "--repo", REPO, "--package", PKG, "--version", "1.44.3"])
    assert code == 0

    closes = fake.commands_containing("close")
    assert len(closes) == 1
    assert "https://example/pr/700" in closes[0]
    assert "--delete-branch" in closes[0]
    assert "Superseded by the bump to 1.44.3." in closes[0]
    # The other package's branch was never touched.
    assert not any("https://example/pr/701" in c for c in fake.calls)


def test_supersede_with_a_newer_open_pr_exits_10_and_closes_nothing(
    monkeypatch: Any, capsys: Any
) -> None:
    prs = [
        {
            "number": 700,
            "headRefName": f"chore/bump-{PKG}-1.40.0",
            "url": "https://example/pr/700",
        },
        {
            "number": 730,
            "headRefName": f"chore/bump-{PKG}-1.45.0",
            "url": "https://example/pr/730",
        },
    ]

    def respond(args: list[str]) -> str | None:
        if "list" in args:
            return _pr_list_json(prs)
        if "close" in args:
            return ""
        return None

    fake = FakeRun(respond)
    _install(monkeypatch, fake)

    code = bump_flow.main(["supersede", "--repo", REPO, "--package", PKG, "--version", "1.44.3"])
    assert code == 10
    assert "newer-open https://example/pr/730" in capsys.readouterr().out
    # Nothing was closed, not even the strictly-older PR.
    assert fake.commands_containing("close") == []


# --- await -------------------------------------------------------------------


def _pr_view(state: str, merge_status: str, sha: str = "abc123") -> str:
    return json.dumps({"state": state, "mergeStateStatus": merge_status, "headRefOid": sha})


def _runs(entries: list[dict[str, Any]]) -> str:
    return json.dumps({"total_count": len(entries), "workflow_runs": entries})


def test_await_blocked_then_blocked_then_merged_exits_0(monkeypatch: Any, capsys: Any) -> None:
    views = iter(
        [
            _pr_view("OPEN", "BLOCKED"),
            _pr_view("OPEN", "BLOCKED"),
            _pr_view("MERGED", "CLEAN"),
        ]
    )
    sleeps: list[float] = []

    def respond(args: list[str]) -> str | None:
        if "view" in args:
            return next(views)
        if "api" in args:  # CI still running while BLOCKED
            return _runs([{"name": "CI", "status": "in_progress", "conclusion": None}])
        return None

    fake = FakeRun(respond)
    monkeypatch.setattr(bump_flow, "_run", fake)
    monkeypatch.setattr(bump_flow, "_now", lambda: 0.0)
    monkeypatch.setattr(bump_flow, "_sleep", lambda s: sleeps.append(s))

    code = bump_flow.main(
        [
            "await",
            "--repo",
            REPO,
            "--package",
            PKG,
            "--pr",
            "https://example/pr/729",
            "--timeout-min",
            "5",
            "--poll-sec",
            "7",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("outcome=merged detail=")
    assert sleeps == [7, 7]  # slept once per pending poll, then merged


def test_await_dirty_is_a_conflict_exit_20(monkeypatch: Any, capsys: Any) -> None:
    def respond(args: list[str]) -> str | None:
        if "view" in args:
            return _pr_view("OPEN", "DIRTY")
        return None

    fake = FakeRun(respond)
    _install(monkeypatch, fake)

    code = bump_flow.main(
        [
            "await",
            "--repo",
            REPO,
            "--package",
            PKG,
            "--pr",
            "https://example/pr/729",
            "--timeout-min",
            "5",
            "--poll-sec",
            "1",
        ]
    )
    assert code == 20
    assert capsys.readouterr().out.startswith("outcome=conflict detail=")


def test_await_failed_ci_exits_30_and_names_the_run(monkeypatch: Any, capsys: Any) -> None:
    def respond(args: list[str]) -> str | None:
        if "view" in args:
            return _pr_view("OPEN", "BLOCKED", sha="deadbeef")
        if "api" in args:
            return _runs(
                [
                    {"name": "Feature Review", "status": "completed", "conclusion": "skipped"},
                    {"name": "CI", "status": "completed", "conclusion": "failure"},
                ]
            )
        return None

    fake = FakeRun(respond)
    _install(monkeypatch, fake)

    code = bump_flow.main(
        [
            "await",
            "--repo",
            REPO,
            "--package",
            PKG,
            "--pr",
            "https://example/pr/729",
            "--timeout-min",
            "5",
            "--poll-sec",
            "1",
        ]
    )
    assert code == 30
    out = capsys.readouterr().out
    assert out.startswith("outcome=failed detail=")
    assert "CI" in out


def test_await_times_out_exits_30(monkeypatch: Any, capsys: Any) -> None:
    """An injected clock: the first read establishes the deadline, the second
    jumps past it, so a PR that never resolves times out rather than looping
    forever."""
    clock = iter([0.0, 10_000.0])

    def respond(args: list[str]) -> str | None:
        if "view" in args:
            return _pr_view("OPEN", "BLOCKED")
        if "api" in args:
            return _runs([{"name": "CI", "status": "in_progress", "conclusion": None}])
        return None

    fake = FakeRun(respond)
    monkeypatch.setattr(bump_flow, "_run", fake)
    monkeypatch.setattr(bump_flow, "_sleep", lambda _s: None)
    monkeypatch.setattr(bump_flow, "_now", lambda: next(clock))

    code = bump_flow.main(
        [
            "await",
            "--repo",
            REPO,
            "--package",
            PKG,
            "--pr",
            "https://example/pr/729",
            "--timeout-min",
            "5",
            "--poll-sec",
            "1",
        ]
    )
    assert code == 30
    assert capsys.readouterr().out.startswith("outcome=timeout detail=")


# --- stall -------------------------------------------------------------------


def test_stall_creates_an_issue_when_none_is_open(monkeypatch: Any, capsys: Any) -> None:
    def respond(args: list[str]) -> str | None:
        if "label" in args and "create" in args:
            return ""
        if "issue" in args and "list" in args:
            return "[]"
        if "issue" in args and "create" in args:
            return "https://example/issues/9\n"
        return None

    fake = FakeRun(respond)
    _install(monkeypatch, fake)

    code = bump_flow.main(
        [
            "stall",
            "--repo",
            REPO,
            "--package",
            PKG,
            "--version",
            "1.44.3",
            "--reason",
            "failed",
            "--pr",
            "https://example/pr/729",
            "--run",
            "https://example/run/1",
        ]
    )
    assert code == 0

    # The label is ensured first.
    assert fake.commands_containing("label", "create", "chain-stall")

    creates = fake.commands_containing("issue", "create")
    assert len(creates) == 1
    body = creates[0]
    assert f"Bump {PKG}: 1.44.3 could not merge" in body
    assert "chain-stall" in body
    # The body carries version, reason, PR, run, and human guidance.
    joined = " ".join(body)
    assert "1.44.3" in joined
    assert "failed" in joined
    assert "https://example/pr/729" in joined
    assert "https://example/run/1" in joined
    assert "contract break" in joined  # the 'failed' guidance
    # No comment path when nothing was open.
    assert fake.commands_containing("issue", "comment") == []


def test_stall_comments_when_an_issue_is_already_open(monkeypatch: Any, capsys: Any) -> None:
    open_issue = [{"number": 42, "title": f"Bump {PKG}: 1.40.0 could not merge"}]

    def respond(args: list[str]) -> str | None:
        if "label" in args and "create" in args:
            return ""
        if "issue" in args and "list" in args:
            return json.dumps(open_issue)
        if "issue" in args and "comment" in args:
            return ""
        return None

    fake = FakeRun(respond)
    _install(monkeypatch, fake)

    code = bump_flow.main(
        [
            "stall",
            "--repo",
            REPO,
            "--package",
            PKG,
            "--version",
            "1.44.3",
            "--reason",
            "conflict",
            "--pr",
            "-",
            "--run",
            "https://example/run/2",
        ]
    )
    assert code == 0

    comments = fake.commands_containing("issue", "comment")
    assert len(comments) == 1
    assert "42" in comments[0]
    # One issue per package: it commented, it did NOT open a second issue.
    assert fake.commands_containing("issue", "create") == []


def test_stall_label_from_config(monkeypatch: Any, tmp_path: Path) -> None:
    """The stall label is a project answer (``stall_label`` in pins.toml), not a
    fact baked in code. With ``--config`` pointing at a config whose label is
    ``custom-stall``, every label-scoped ``gh`` call must use that label."""
    config = tmp_path / "pins.toml"
    config.write_text(
        'lock_command = ""\n'
        'post_lock_command = ""\n'
        'stall_label = "custom-stall"\n'
        "[[package]]\n"
        f'name = "{PKG}"\n'
        'files = ["pyproject.toml"]\n'
    )

    def respond(args: list[str]) -> str | None:
        if "label" in args and "create" in args:
            return ""
        if "issue" in args and "list" in args:
            return "[]"
        if "issue" in args and "create" in args:
            return "https://example/issues/9\n"
        return None

    fake = FakeRun(respond)
    _install(monkeypatch, fake)

    code = bump_flow.main(
        [
            "stall",
            "--repo",
            REPO,
            "--package",
            PKG,
            "--version",
            "1.44.3",
            "--reason",
            "failed",
            "--pr",
            "-",
            "--run",
            "https://example/run/3",
            "--config",
            str(config),
        ]
    )
    assert code == 0

    # The custom label is used, and the default is not.
    assert fake.commands_containing("label", "create", "custom-stall")
    assert fake.commands_containing("issue", "create", "custom-stall")
    assert fake.commands_containing("label", "create", "chain-stall") == []


# --- resolve -----------------------------------------------------------------


def test_resolve_closes_the_open_issue(monkeypatch: Any, capsys: Any) -> None:
    open_issue = [{"number": 42, "title": f"Bump {PKG}: 1.40.0 could not merge"}]

    def respond(args: list[str]) -> str | None:
        if "issue" in args and "list" in args:
            return json.dumps(open_issue)
        if "issue" in args and "close" in args:
            return ""
        if "issue" in args and "comment" in args:
            return ""
        return None

    fake = FakeRun(respond)
    _install(monkeypatch, fake)

    code = bump_flow.main(
        [
            "resolve",
            "--repo",
            REPO,
            "--package",
            PKG,
            "--version",
            "1.44.3",
            "--pr",
            "https://example/pr/729",
        ]
    )
    assert code == 0

    closes = fake.commands_containing("issue", "close")
    assert len(closes) == 1
    assert "42" in closes[0]
    assert any(f"Resolved: {PKG} 1.44.3 merged via https://example/pr/729" in c for c in closes[0])


def test_resolve_does_nothing_when_no_issue_is_open(monkeypatch: Any, capsys: Any) -> None:
    def respond(args: list[str]) -> str | None:
        if "issue" in args and "list" in args:
            return "[]"
        return None

    fake = FakeRun(respond)
    _install(monkeypatch, fake)

    code = bump_flow.main(
        [
            "resolve",
            "--repo",
            REPO,
            "--package",
            PKG,
            "--version",
            "1.44.3",
            "--pr",
            "https://example/pr/729",
        ]
    )
    assert code == 0
    assert fake.commands_containing("issue", "close") == []
    assert fake.commands_containing("issue", "comment") == []


# --- the one subprocess site -------------------------------------------------


def test_run_raises_on_non_zero_exit() -> None:
    """The single `_run` site is the only place a subprocess is spawned, and a
    non-zero exit must raise — a silently-ignored `gh` failure is how the chain
    would 'succeed' having closed nothing and opened nothing."""
    with pytest.raises(bump_flow.CommandError):
        bump_flow._run(["sh", "-c", "exit 3"])


def test_run_returns_stdout_on_success() -> None:
    assert bump_flow._run(["printf", "hello"]) == "hello"


# --- stall label validation (no silent fallback; M3) --------------------------


def test_stall_label_defaults_only_when_no_config() -> None:
    """The built-in default applies ONLY when no --config is supplied (offline)."""
    assert bump_flow._stall_label(None) == bump_flow.STALL_LABEL


def test_stall_label_missing_key_raises_and_names_it(tmp_path: Path) -> None:
    config = tmp_path / "pins.toml"
    config.write_text('[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n')
    with pytest.raises(bump_flow.ConfigError) as exc:
        bump_flow._stall_label(config)
    assert "stall_label" in str(exc.value) and "missing" in str(exc.value)


def test_stall_label_empty_raises_not_defaults(tmp_path: Path) -> None:
    """An empty stall_label is a misconfiguration, not a request for the default —
    it must fail loudly with a ConfigError, never silently fall back."""
    config = tmp_path / "pins.toml"
    config.write_text('stall_label = ""\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n')
    with pytest.raises(bump_flow.ConfigError) as exc:
        bump_flow._stall_label(config)
    assert "stall_label" in str(exc.value) and "empty" in str(exc.value)


def test_stall_label_wrong_type_raises_and_names_it(tmp_path: Path) -> None:
    config = tmp_path / "pins.toml"
    config.write_text('stall_label = 123\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n')
    with pytest.raises(bump_flow.ConfigError) as exc:
        bump_flow._stall_label(config)
    assert "stall_label" in str(exc.value) and "string" in str(exc.value)


def test_main_stall_empty_label_exits_1_and_names_it(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    """The empty-label ConfigError surfaces as a `::error::` + exit 1 through main,
    rather than crashing or silently stalling under the default label."""
    config = tmp_path / "pins.toml"
    config.write_text('stall_label = ""\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n')
    fake = FakeRun(lambda args: None)
    _install(monkeypatch, fake)

    code = bump_flow.main(
        [
            "stall",
            "--repo",
            REPO,
            "--package",
            PKG,
            "--version",
            "1.44.3",
            "--reason",
            "failed",
            "--pr",
            "-",
            "--run",
            "https://example/run/3",
            "--config",
            str(config),
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "::error::" in err and "stall_label" in err and "empty" in err
    # No gh call was made — it failed before touching GitHub.
    assert fake.calls == []


# --- required_check is a pins.toml setting (I5) ------------------------------


def test_required_check_defaults_to_CI_when_no_config() -> None:
    """The built-in default applies ONLY when no --config is supplied (offline)."""
    assert bump_flow._required_check(None) == bump_flow.REQUIRED_CHECK == "CI"


def test_required_check_read_from_config(tmp_path: Path) -> None:
    config = tmp_path / "pins.toml"
    config.write_text(
        'required_check = "build-and-test"\n'
        'stall_label = "chain-stall"\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n'
    )
    assert bump_flow._required_check(config) == "build-and-test"


def test_required_check_missing_key_raises(tmp_path: Path) -> None:
    config = tmp_path / "pins.toml"
    config.write_text('stall_label = "chain-stall"\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n')
    with pytest.raises(bump_flow.ConfigError) as exc:
        bump_flow._required_check(config)
    assert "required_check" in str(exc.value) and "missing" in str(exc.value)


def test_required_check_empty_raises(tmp_path: Path) -> None:
    config = tmp_path / "pins.toml"
    config.write_text('required_check = ""\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n')
    with pytest.raises(bump_flow.ConfigError) as exc:
        bump_flow._required_check(config)
    assert "required_check" in str(exc.value) and "empty" in str(exc.value)


def test_required_check_wrong_type_raises(tmp_path: Path) -> None:
    config = tmp_path / "pins.toml"
    config.write_text('required_check = 5\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n')
    with pytest.raises(bump_flow.ConfigError) as exc:
        bump_flow._required_check(config)
    assert "required_check" in str(exc.value) and "string" in str(exc.value)


def test_await_uses_the_configured_required_check(monkeypatch: Any, tmp_path: Path) -> None:
    """A red run of the CONFIGURED check name (not the hardcoded 'CI') stalls; a
    red run named 'CI' is ignored when the configured gate is something else."""
    config = tmp_path / "pins.toml"
    config.write_text(
        'required_check = "build-and-test"\n'
        'stall_label = "chain-stall"\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n'
    )

    def respond(args: list[str]) -> str | None:
        if "view" in args:
            return _pr_view("OPEN", "BLOCKED", sha="cafef00d")
        if "api" in args:
            return _runs(
                [
                    {"name": "CI", "status": "completed", "conclusion": "failure"},
                    {"name": "build-and-test", "status": "completed", "conclusion": "failure"},
                ]
            )
        return None

    fake = FakeRun(respond)
    _install(monkeypatch, fake)

    code = bump_flow.main(
        [
            "await",
            "--repo",
            REPO,
            "--package",
            PKG,
            "--pr",
            "https://example/pr/1",
            "--timeout-min",
            "5",
            "--poll-sec",
            "1",
            "--config",
            str(config),
        ]
    )
    assert code == 30  # failed -> unmerged


def test_await_ignores_a_red_CI_when_the_gate_is_named_otherwise(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    config = tmp_path / "pins.toml"
    config.write_text(
        'required_check = "build-and-test"\n'
        'stall_label = "chain-stall"\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n'
    )
    clock = iter([0.0, 10_000.0])

    def respond(args: list[str]) -> str | None:
        if "view" in args:
            return _pr_view("OPEN", "BLOCKED")
        if "api" in args:
            # 'CI' is red but is NOT the configured gate -> not a failure.
            return _runs([{"name": "CI", "status": "completed", "conclusion": "failure"}])
        return None

    fake = FakeRun(respond)
    monkeypatch.setattr(bump_flow, "_run", fake)
    monkeypatch.setattr(bump_flow, "_sleep", lambda _s: None)
    monkeypatch.setattr(bump_flow, "_now", lambda: next(clock))

    code = bump_flow.main(
        [
            "await", "--repo", REPO, "--package", PKG, "--pr", "https://example/pr/1",
            "--timeout-min", "5", "--poll-sec", "1", "--config", str(config),
        ]
    )
    assert code == 30
    assert capsys.readouterr().out.startswith("outcome=timeout")


# --- stall path is resilient before checkout / to a bad config (I1) ----------


def _stall_argv(config: str | None, label: str | None) -> list[str]:
    argv = [
        "stall", "--repo", REPO, "--package", PKG, "--version", "1.44.3",
        "--reason", "workflow step failed: mint-app-token", "--pr", "-",
        "--run", "https://example/run/9",
    ]
    if config is not None:
        argv += ["--config", config]
    if label is not None:
        argv += ["--stall-label", label]
    return argv


def _stall_responder() -> Callable[[list[str]], str | None]:
    def respond(args: list[str]) -> str | None:
        if "label" in args and "create" in args:
            return ""
        if "issue" in args and "list" in args:
            return "[]"
        if "issue" in args and "create" in args:
            return "https://example/issues/9\n"
        return None

    return respond


def test_stall_files_issue_when_config_is_absent_using_fallback_label(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """A failure BEFORE checkout leaves pins.toml absent. The stall issue is still
    filed, under the fallback stall_label, and the body records the config error."""
    fake = FakeRun(_stall_responder())
    _install(monkeypatch, fake)
    missing = str(tmp_path / "__absent" / "pins.toml")

    code = bump_flow.main(_stall_argv(missing, "chain-stall"))
    assert code == 0
    creates = fake.commands_containing("issue", "create")
    assert len(creates) == 1
    body = " ".join(creates[0])
    assert "chain-stall" in body
    assert "Config error" in body and "unreadable" in body
    assert fake.commands_containing("label", "create", "chain-stall")


def test_stall_files_issue_when_config_is_malformed_toml(
    monkeypatch: Any, tmp_path: Any
) -> None:
    fake = FakeRun(_stall_responder())
    _install(monkeypatch, fake)
    bad = tmp_path / "pins.toml"
    bad.write_text('this is = not valid = toml [[[\n')

    code = bump_flow.main(_stall_argv(str(bad), "fallback-label"))
    assert code == 0
    creates = fake.commands_containing("issue", "create")
    assert len(creates) == 1
    body = " ".join(creates[0])
    assert "fallback-label" in body
    assert "Config error" in body and "malformed" in body


def test_stall_uses_the_config_label_when_config_is_readable(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """When pins.toml IS readable, the label comes from it (not the fallback) and
    no config-error note is added."""
    fake = FakeRun(_stall_responder())
    _install(monkeypatch, fake)
    config = tmp_path / "pins.toml"
    config.write_text(
        'stall_label = "from-config"\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n'
    )

    code = bump_flow.main(_stall_argv(str(config), "fallback-label"))
    assert code == 0
    creates = fake.commands_containing("issue", "create")
    body = " ".join(creates[0])
    assert "from-config" in body
    assert "Config error" not in body
    assert fake.commands_containing("label", "create", "from-config")


def test_stall_malformed_config_without_fallback_still_raises(tmp_path: Any) -> None:
    """No fallback (offline callers): the strict path still raises on a bad label,
    so the resilient behaviour is opt-in via --stall-label."""
    config = tmp_path / "pins.toml"
    config.write_text('stall_label = ""\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n')
    with pytest.raises(bump_flow.ConfigError):
        bump_flow._stall_label_resilient(config, None)
