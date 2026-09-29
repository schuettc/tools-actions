#!/usr/bin/env python3
"""THE decision logic for the automated pin-bump chain — tested, offline.

The rendered ``bump-*.yml`` caller workflow opens a PR that rewrites a consumer
pin (see :mod:`bump_pin`), then hands off to *this* module for everything that
is a *decision* rather than a rewrite:

* **supersede** — an older open bump PR for the same package is closed and its
  branch deleted, because a newer version has arrived. But if a *newer* bump is
  already open, this one steps aside (exit 10) and closes nothing: racing to
  close the newer PR is how a chain drops the version it was supposed to land.
* **await** — block on a single PR until it reaches an outcome, distinguishing a
  merge from a mergeability conflict (`DIRTY`/`BEHIND`, regenerated upstream)
  from a red required check from a timeout. CI status is read with the
  workflow's `GITHUB_TOKEN` (`actions: read`), because the release-bot App token
  cannot see `statusCheckRollup` (F-0812t-coord).
* **stall** — open or update a single stall issue per package so no hand-off
  failure is silent (global constraint: "no silent stall"). The label is read
  from ``pins.toml`` (``--config``), so it is a project answer, not a fact baked
  into this code.
* **resolve** — close that issue once the bump finally merges.

Like :mod:`bump_pin`, this is **stdlib only** (Python 3.12+; ``tomllib`` is
stdlib from 3.11) and invoked as a plain script (``python3 ci/bump/bump_flow.py``).
Every subprocess goes through the ONE :func:`_run` site, which raises on a
non-zero exit — a silently-ignored `gh` failure is how the chain would "succeed"
having closed nothing and opened nothing. Tests monkeypatch that one site and
inject the clock/sleep, so nothing here ever reaches GitHub in CI.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

# --- exit codes (matched as literals by the calling workflows) ---------------

#: `await`: the PR merged. The only success.
EXIT_MERGED = 0
#: `supersede`: a newer bump PR is already open; this run stepped aside.
EXIT_NEWER_OPEN = 10
#: `await`: the PR is un-mergeable (`DIRTY`/`BEHIND`) — the branch needs
#: regenerating. A distinct code so the loop can regenerate and retry instead of
#: giving up.
EXIT_CONFLICT = 20
#: `await`: closed, failed, or timed out — an unmerged outcome that stalls.
EXIT_UNMERGED = 30

#: outcome name → process exit code.
OUTCOME_EXIT: dict[str, int] = {
    "merged": EXIT_MERGED,
    "conflict": EXIT_CONFLICT,
    "closed": EXIT_UNMERGED,
    "failed": EXIT_UNMERGED,
    "timeout": EXIT_UNMERGED,
}

#: The DEFAULT required-check name, used ONLY when no ``--config`` is supplied
#: (the offline tests). At runtime the workflow always passes ``--config`` and the
#: name is a project answer (``required_check`` in pins.toml): a consumer whose
#: merge gate is not named ``CI`` would otherwise stall on a 20-minute "timeout"
#: instead of reading its own red check. A non-required run going red (e.g. an
#: advisory review workflow, which reports `skipped` on bot PRs) must NOT be read
#: as a failure, or every bump would stall on an advisory check.
REQUIRED_CHECK = "CI"

#: The default label that marks a chain hand-off failure when no ``pins.toml`` is
#: supplied (the offline tests). At runtime the workflow always passes
#: ``--config``, so the label is a project answer (``stall_label`` in pins.toml),
#: never a fact baked here.
STALL_LABEL = "chain-stall"

#: reason prefix → what a human should actually do. Keyed on prefix so the
#: caller can pass a richer reason string (`"failed: CI red on lint"`) and still
#: match. The prefixes line up with `await`'s outcome names.
REASON_GUIDANCE: dict[str, str] = {
    "failed": (
        "CI failed on the bump PR — likely a contract break; fix on the "
        "producer side or close the PR."
    ),
    "conflict": (
        "The bump PR is out of date with the base branch (DIRTY/BEHIND). "
        "Regenerate the branch from the current base and re-run the bump."
    ),
    "closed": (
        "The bump PR was closed without merging. Reopen and land it, or record "
        "here why the bump was abandoned."
    ),
    "timeout": (
        "The bump PR never reached an outcome in the allotted time. Check CI "
        "health, then merge or close it by hand."
    ),
    "superseded": (
        "A newer bump for this package is open. Confirm the newer PR lands; "
        "this one was intentionally left alone."
    ),
}

#: Used when no prefix matched — never silently omit guidance.
DEFAULT_GUIDANCE = (
    "Investigate the bump PR and either merge it or close the chain by hand, "
    "then resolve this issue."
)


class CommandError(RuntimeError):
    """A subprocess exited non-zero — carries argv, stdout, and stderr."""


class ConfigError(Exception):
    """``pins.toml`` is missing a required key or carries a wrong-typed/empty
    value. Names the offender — no silent fallback to a value the workflow does
    not agree with."""


def _run(args: list[str]) -> str:
    """THE one subprocess site. Returns stdout; raises on a non-zero exit.

    Every `gh` call in this module funnels through here so that (a) tests
    monkeypatch a single seam and (b) a `gh` failure can never be silently
    swallowed into a "successful" no-op.
    """
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise CommandError(f"command {args} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _gh_json(args: list[str]) -> Any:
    """Run a `gh` command that emits JSON and parse it."""
    return json.loads(_run(args))


def _required_check(config: Path | None) -> str:
    """The required-check name — read from ``pins.toml`` (``--config``).

    Mirrors :func:`_stall_label`: the built-in :data:`REQUIRED_CHECK` default
    applies ONLY when no ``--config`` is supplied (the offline tests). When a
    config IS supplied the ``required_check`` key must be present, a string, and
    non-empty — a missing/empty/mistyped value is a loud :class:`ConfigError`, so
    a consumer never silently inherits a gate name (`CI`) it does not use.
    """
    if config is None:
        return REQUIRED_CHECK
    data = tomllib.loads(config.read_text())
    if "required_check" not in data:
        raise ConfigError(f"{config}: missing required key 'required_check'")
    value = data["required_check"]
    if not isinstance(value, str):
        raise ConfigError(
            f"{config}: 'required_check' must be a string, got {type(value).__name__}"
        )
    if not value:
        raise ConfigError(
            f"{config}: 'required_check' is empty — set a non-empty check name "
            "(no silent fallback to the built-in default)."
        )
    return value


def _stall_label(config: Path | None) -> str:
    """The stall-issue label — read from ``pins.toml`` (``--config``).

    The built-in :data:`STALL_LABEL` default applies ONLY when no ``--config`` is
    supplied (the offline tests); at runtime the workflow always passes
    ``--config``, so the label is a project answer. When a config IS supplied the
    key must be present, a string, and non-empty:

    * a **missing** ``stall_label`` key raises — no silent fallback to a label
      the workflow does not agree with;
    * an **empty** (or non-string) ``stall_label`` raises for the same reason: an
      empty value is a misconfiguration, not a request for the default. Failing
      loudly here is the whole point of reading the label from config.
    """
    if config is None:
        return STALL_LABEL
    data = tomllib.loads(config.read_text())
    if "stall_label" not in data:
        raise ConfigError(f"{config}: missing required key 'stall_label'")
    label = data["stall_label"]
    if not isinstance(label, str):
        raise ConfigError(
            f"{config}: 'stall_label' must be a string, got {type(label).__name__}"
        )
    if not label:
        raise ConfigError(
            f"{config}: 'stall_label' is empty — set a non-empty label "
            "(no silent fallback to the built-in default)."
        )
    return label


def _stall_label_resilient(config: Path | None, fallback: str | None) -> tuple[str, str | None]:
    """The stall-issue label for the STALL path, which MUST always open an issue.

    The stall step can run before the consumer checkout (a validate / App-secret /
    token-mint failure) or against a missing or malformed ``pins.toml``. In those
    cases the strict :func:`_stall_label` would raise an uncaught error and NO
    issue would be filed — the exact silent-stall this whole path exists to
    prevent. So the stall path degrades: if the config is unreadable (absent,
    unreadable, or malformed TOML) and a ``fallback`` label is supplied (the
    ``stall_label`` workflow input), the issue is filed under the fallback and the
    config error is returned to be recorded IN THE BODY — never a silent default.

    Returns ``(label, config_note_or_None)``. With no ``fallback`` supplied this is
    the strict :func:`_stall_label` (it raises), so the offline callers are
    unchanged.
    """
    if fallback is None:
        return _stall_label(config), None
    if config is None:
        return fallback, None
    try:
        text = config.read_text()
    except OSError as exc:
        return fallback, f"pins config unreadable ({config}): {exc}"
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return fallback, f"pins config malformed TOML ({config}): {exc}"
    try:
        return _stall_label(config), None
    except ConfigError as exc:
        return fallback, str(exc)


#: Injected in tests. The clock is `monotonic` (immune to wall-clock jumps) and
#: sleep is the real one; both are module-level so a test can replace them
#: without threading parameters through `main`.
def _now() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _parse_version(version: str) -> tuple[int, ...]:
    """`"1.10.0"` → `(1, 10, 0)`. Integer tuples so `1.9.0 < 1.10.0` holds — a
    lexical compare would sort `1.10.0` before `1.9.0` and supersede the wrong
    PR."""
    return tuple(int(part) for part in version.split("."))


# --- supersede ---------------------------------------------------------------


def _bump_branch_re(package: str) -> re.Pattern[str]:
    return re.compile(rf"^chore/bump-{re.escape(package)}-(\d+\.\d+\.\d+)$")


def _supersede(repo: str, package: str, version: str) -> int:
    """Close strictly-older open bump PRs for ``package``; step aside for newer.

    Ordering matters: the newer-open check happens BEFORE any close, so a run
    that has been overtaken closes nothing at all.
    """
    target = _parse_version(version)
    pattern = _bump_branch_re(package)
    prs = _gh_json(
        ["gh", "pr", "list", "--repo", repo, "--state", "open", "--json", "number,headRefName,url"]
    )

    matched: list[tuple[tuple[int, ...], dict[str, Any]]] = []
    for pr in prs:
        m = pattern.match(pr["headRefName"])
        if m is not None:
            matched.append((_parse_version(m.group(1)), pr))

    newer = [pr for ver, pr in matched if ver > target]
    if newer:
        for pr in newer:
            print(f"newer-open {pr['url']}")
        return EXIT_NEWER_OPEN

    for ver, pr in matched:
        if ver < target:
            _run(
                [
                    "gh",
                    "pr",
                    "close",
                    pr["url"],
                    "--comment",
                    f"Superseded by the bump to {version}.",
                    "--delete-branch",
                ]
            )
    return 0


# --- await -------------------------------------------------------------------


def _failed_required_runs(repo: str, sha: str, required_check: str) -> list[str]:
    """Names of required-check workflow runs at ``sha`` whose conclusion failed.

    Reads CI with the workflow token (`actions: read`); the App token cannot see
    the check rollup (F-0812t-coord). Only ``required_check`` runs count, so
    an advisory workflow's failure never stalls a bump.
    """
    data = _gh_json(["gh", "api", f"repos/{repo}/actions/runs?head_sha={sha}"])
    return [
        run["name"]
        for run in data["workflow_runs"]
        if run["name"] == required_check and run["conclusion"] == "failure"
    ]


def _poll_once(repo: str, pr_url: str, required_check: str) -> tuple[str, str] | None:
    """One read of the PR. Returns ``(outcome, detail)`` or ``None`` if pending."""
    data = _gh_json(["gh", "pr", "view", pr_url, "--json", "state,mergeStateStatus,headRefOid"])
    state = data["state"]
    if state == "MERGED":
        return "merged", pr_url
    if state == "CLOSED":
        return "closed", "PR closed without merging"

    merge_status = data["mergeStateStatus"]
    if merge_status in {"DIRTY", "BEHIND"}:
        return "conflict", f"mergeStateStatus={merge_status}"

    failed = _failed_required_runs(repo, data["headRefOid"], required_check)
    if failed:
        return "failed", f"required check failed: {', '.join(failed)}"

    return None


def _await_pr(
    repo: str, pr_url: str, timeout_min: int, poll_sec: int, required_check: str
) -> tuple[str, str]:
    """Poll ``pr_url`` until an outcome or the timeout. Returns ``(outcome, detail)``."""
    deadline = _now() + timeout_min * 60
    while True:
        result = _poll_once(repo, pr_url, required_check)
        if result is not None:
            return result
        if _now() >= deadline:
            return "timeout", f"no outcome after {timeout_min} min"
        _sleep(poll_sec)


# --- stall / resolve ---------------------------------------------------------


def _stall_title_prefix(package: str) -> str:
    return f"Bump {package}:"


def _open_stall_issue(repo: str, package: str, label: str) -> int | None:
    """Number of the open stall issue for ``package``, or ``None``.

    One issue per package: filter by the label AND by the title prefix so two
    packages stalling at once do not comment on each other's issue.
    """
    prefix = _stall_title_prefix(package)
    issues = _gh_json(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            label,
            "--json",
            "number,title",
        ]
    )
    for issue in issues:
        if issue["title"].startswith(prefix):
            return int(issue["number"])
    return None


def _guidance(reason: str) -> str:
    for prefix, text in REASON_GUIDANCE.items():
        if reason.startswith(prefix):
            return text
    return DEFAULT_GUIDANCE


def _stall_body(
    package: str, version: str, reason: str, pr: str, run: str, config_note: str | None = None
) -> str:
    pr_line = pr if pr != "-" else "(no PR was opened)"
    lines = [
        f"- **Package:** {package}",
        f"- **Version:** {version}",
        f"- **Reason:** {reason}",
        f"- **PR:** {pr_line}",
        f"- **CI run:** {run}",
    ]
    if config_note:
        # The stall label came from the workflow fallback, not pins.toml, because
        # the config was unreadable — record why so the misconfiguration is fixed
        # rather than the fallback label quietly masking it.
        lines.append(f"- **Config error:** {config_note}")
    lines += ["", f"**What to do:** {_guidance(reason)}"]
    return "\n".join(lines)


def _ensure_label(repo: str, label: str) -> None:
    _run(
        [
            "gh",
            "label",
            "create",
            label,
            "--repo",
            repo,
            "--color",
            "B60205",
            "--description",
            "Automated chain hand-off failed",
            "--force",
        ]
    )


def _stall(
    repo: str,
    package: str,
    version: str,
    reason: str,
    pr: str,
    run: str,
    label: str,
    config_note: str | None = None,
) -> int:
    """Open a stall issue for ``package``, or comment if one is open."""
    _ensure_label(repo, label)
    body = _stall_body(package, version, reason, pr, run, config_note)
    number = _open_stall_issue(repo, package, label)
    if number is not None:
        _run(["gh", "issue", "comment", str(number), "--repo", repo, "--body", body])
        return 0
    _run(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            f"{_stall_title_prefix(package)} {version} could not merge",
            "--label",
            label,
            "--body",
            body,
        ]
    )
    return 0


def _resolve(repo: str, package: str, version: str, pr: str, label: str) -> int:
    """Close the open stall issue for ``package``, if any."""
    number = _open_stall_issue(repo, package, label)
    if number is None:
        return 0
    _run(
        [
            "gh",
            "issue",
            "close",
            str(number),
            "--repo",
            repo,
            "--comment",
            f"Resolved: {package} {version} merged via {pr}",
        ]
    )
    return 0


# --- CLI ---------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    parser.add_argument("--package", required=True)


def main(argv: list[str] | None = None) -> int:
    """Dispatch a chain subcommand. See the module docstring for each."""
    parser = argparse.ArgumentParser(
        prog="bump_flow",
        description="Decision logic for the automated pin-bump chain.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_supersede = sub.add_parser("supersede", help="close older open bump PRs")
    _add_common(p_supersede)
    p_supersede.add_argument("--version", required=True)

    p_await = sub.add_parser("await", help="block until a PR reaches an outcome")
    _add_common(p_await)
    p_await.add_argument("--pr", required=True, help="PR URL")
    p_await.add_argument("--timeout-min", type=int, required=True)
    p_await.add_argument("--poll-sec", type=int, required=True)
    p_await.add_argument("--config", type=Path, help="pins.toml (for the required-check name)")

    p_stall = sub.add_parser("stall", help="open or update the stall issue")
    _add_common(p_stall)
    p_stall.add_argument("--version", required=True)
    p_stall.add_argument("--reason", required=True)
    p_stall.add_argument("--pr", required=True, help="PR URL, or - if none")
    p_stall.add_argument("--run", required=True, help="CI run URL")
    p_stall.add_argument("--config", type=Path, help="pins.toml (for the stall label)")
    p_stall.add_argument(
        "--stall-label",
        help=(
            "fallback label used ONLY when --config is unreadable (a pre-checkout "
            "failure or a missing/malformed pins.toml), so a stall issue is filed "
            "even then — never a silent default"
        ),
    )

    p_resolve = sub.add_parser("resolve", help="close the stall issue on merge")
    _add_common(p_resolve)
    p_resolve.add_argument("--version", required=True)
    p_resolve.add_argument("--pr", required=True, help="PR URL")
    p_resolve.add_argument("--config", type=Path, help="pins.toml (for the stall label)")

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        if args.command == "supersede":
            return _supersede(args.repo, args.package, args.version)
        if args.command == "await":
            required_check = _required_check(args.config)
            outcome, detail = _await_pr(
                args.repo, args.pr, args.timeout_min, args.poll_sec, required_check
            )
            print(f"outcome={outcome} detail={detail}")
            return OUTCOME_EXIT[outcome]
        if args.command == "stall":
            label, config_note = _stall_label_resilient(args.config, args.stall_label)
            return _stall(
                args.repo,
                args.package,
                args.version,
                args.reason,
                args.pr,
                args.run,
                label,
                config_note,
            )
        if args.command == "resolve":
            label = _stall_label(args.config)
            return _resolve(args.repo, args.package, args.version, args.pr, label)
    except (CommandError, ConfigError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
