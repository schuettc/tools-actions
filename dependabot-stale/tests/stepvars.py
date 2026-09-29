"""A static guard that every shell variable a ``run:`` step dereferences is
actually defined before it is read.

Why this exists
---------------
A composite action has no job-level ``env:``, so a value the source workflow
carried as a job env var (``BASE_BRANCH``) had to be redistributed onto the
step ``env:`` blocks by hand. One was dropped. Because every ``run:`` step opens
``set -u`` (``set -euo pipefail``), the first dereference of that missing var
(``${BASE_BRANCH}``) aborts the step with ``unbound variable`` — on EVERY run,
before anything happens. ``actionlint`` and ``shellcheck`` do not resolve
GitHub's ``env:`` / ``$GITHUB_ENV`` model, so neither flags it. This module does.

What it checks
--------------
For every ``run:`` step in an action / reusable workflow it collects every shell
variable dereferenced as ``$NAME`` or ``${NAME...}`` whose name is UPPERCASE
(``[A-Z_][A-Z0-9_]*``), and asserts each such *bare* reference is satisfied by
one of:

* that step's ``env:`` (plus any job-level / workflow-level ``env:``);
* an EARLIER step writing ``NAME=`` to ``$GITHUB_ENV`` (GitHub propagates those
  to later steps of the same job);
* a variable assigned or exported inside the same script (including ``for`` /
  ``read`` loop variables and inline ``NAME=value cmd`` prefixes);
* GitHub's documented default environment variables (the allowlist below).

A reference that uses a default / alternate / error parameter-expansion operator
— ``${NAME:-x}``, ``${NAME:=x}``, ``${NAME:?x}``, ``${NAME:+x}`` and the
colon-less ``-``/``=``/``?``/``+`` forms — is *inherently* safe under ``set -u``
(it cannot raise "unbound variable"), so that occurrence is NOT required to be
defined. This is per-occurrence only: ``${X:-}`` does not define ``X``, so a
later BARE ``$X`` in the same script is still required to be defined — the
guarding form protects only the spot it is written. The two assigning forms
``${X:=v}`` and ``${X=v}`` are the exception: they actually set ``X``, so later
bare uses in the same script are satisfied. The class of bug this guards against
— a bare, never-guarded, never-defined variable — is still caught.

The parser is importable so other actions (e.g. ``images``) can adopt the same
guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# --- GitHub's documented default environment variables ------------------------
# https://docs.github.com/actions/learn-github-actions/variables#default-environment-variables
# Every default GitHub var is GITHUB_* or RUNNER_*; the rest are the standard
# POSIX / CI shell vars the runner exports. Prefixes cover the whole families so
# a new GITHUB_*/RUNNER_* var never needs an edit here.
_ALLOWED_PREFIXES = ("GITHUB_", "RUNNER_")
_ALLOWED_NAMES = frozenset(
    {
        "CI",
        "HOME",
        "PATH",
        "PWD",
        "OLDPWD",
        "SHELL",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "TZ",
        "HOSTNAME",
        "SHLVL",
    }
)


def _is_default_env(name: str) -> bool:
    return name.startswith(_ALLOWED_PREFIXES) or name in _ALLOWED_NAMES


# A parameter expansion whose operator supplies a default / alternate / test —
# ``${NAME:-x}``, ``${NAME-x}``, ``${NAME:=x}``, ``${NAME:?}``, ``${NAME:+x}`` …
# — cannot raise "unbound variable" under ``set -u``.
_GUARD_OPERATORS = (":-", ":=", ":?", ":+", "-", "=", "?", "+")
# The two operators that ASSIGN the variable as a side effect (``${X:=v}`` and
# the colon-less ``${X=v}``): unlike ``:-``/``:?``/``:+``, these actually define
# ``X``, so a later bare ``$X`` in the same script is satisfied.
_ASSIGN_EXPANSION_OPERATORS = (":=", "=")

# ``$NAME`` (no braces): always a bare reference.
_BARE_SIMPLE = re.compile(r"\$([A-Z_][A-Z0-9_]*)")
# ``${NAME...}``: name plus whatever expansion syntax follows, captured to decide
# whether it is guarded.
_BRACED = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)([^}]*)\}")

# A local assignment: line start OR after a shell separator / ``$(``, an optional
# ``export``/``local``/``declare``/``readonly``, the NAME, an optional array
# subscript, an optional ``+``, then a single ``=`` (never ``==``).
_ASSIGN = re.compile(
    r"(?:^|[\s;&|(])(?:export\s+|local\s+|declare\s+|readonly\s+)?"
    r"([A-Z_][A-Z0-9_]*)(?:\[[^\]]*\])?\+?=(?!=)",
    re.MULTILINE,
)
_FOR_LOOP = re.compile(r"\bfor\s+([A-Z_][A-Z0-9_]*)\s+in\b")
_READ = re.compile(r"\bread\b((?:\s+-\w+)*)((?:\s+[A-Za-z_][A-Za-z0-9_]*)+)")
# A ``NAME=`` assignment as it appears in an env-write output: an ``echo``, a
# Python ``print`` of an f/plain string, or a bare heredoc body line. These only
# count as ``$GITHUB_ENV`` writes when the producing output is redirected there
# (see ``_env_writes``); being in the same script is not enough.
_ENV_ECHO = re.compile(r"""echo\s+["']?([A-Z_][A-Z0-9_]*)=""")
_ENV_PRINT = re.compile(r"""print\(f?["']([A-Z_][A-Z0-9_]*)=""")
_ENV_PLAIN = re.compile(r"""^\s*([A-Z_][A-Z0-9_]*)=""")
# A redirect whose target is exactly ``$GITHUB_ENV`` (``>`` or ``>>``); the
# negative lookahead keeps ``$GITHUB_STEP_SUMMARY`` / ``$GITHUB_ENVIRONMENT``
# from matching.
_ENV_REDIR = re.compile(r">>?\s*\"?\$\{?GITHUB_ENV\}?(?!\w)")
# A heredoc opener: ``<<WORD`` / ``<<-WORD`` / ``<<'WORD'`` / ``<<"WORD"``.
_HEREDOC_START = re.compile(r"<<-?\s*[\"']?(?P<delim>[A-Za-z_][A-Za-z0-9_]*)[\"']?")
# Strip GitHub Actions ``${{ ... }}`` expressions before scanning a run body so
# they are never mistaken for shell ``${...}`` expansions.
_GHA_EXPR = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


def _strip_gha_expressions(script: str) -> str:
    return _GHA_EXPR.sub("", script)


def _local_definitions(script: str) -> set[str]:
    """Names a script defines for itself: assignments, ``for`` and ``read`` vars,
    and the assigning parameter-expansions ``${X:=v}`` / ``${X=v}``."""
    names: set[str] = set(_ASSIGN.findall(script))
    names.update(_FOR_LOOP.findall(script))
    for _flags, targets in _READ.findall(script):
        for tok in targets.split():
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", tok):
                names.add(tok)
    for name, rest in _BRACED.findall(script):
        if rest.startswith(_ASSIGN_EXPANSION_OPERATORS):
            names.add(name)
    return names


def _bare_references(script: str) -> set[str]:
    """Uppercase names dereferenced without a guarding operator."""
    refs: set[str] = set(_BARE_SIMPLE.findall(script))
    for name, rest in _BRACED.findall(script):
        if not rest.startswith(_GUARD_OPERATORS):
            refs.add(name)
    return refs


def _env_write_names(text: str) -> set[str]:
    """``NAME=`` assignments carried by a single output line/body line."""
    names: set[str] = set(_ENV_ECHO.findall(text))
    names.update(_ENV_PRINT.findall(text))
    m = _ENV_PLAIN.match(text)
    if m:
        names.add(m.group(1))
    return names


def _env_writes(script: str) -> set[str]:
    """Names this script writes to ``$GITHUB_ENV`` (available to LATER steps).

    A ``NAME=`` only counts when the output that carries it is redirected to
    ``$GITHUB_ENV``: a one-line ``echo "NAME=..." >> "$GITHUB_ENV"``, a heredoc
    or a ``{ ... }`` group whose redirect targets ``$GITHUB_ENV``. A ``NAME=``
    that lands on ``$GITHUB_STEP_SUMMARY`` (or anywhere else) does not count,
    even if the same script also writes ``$GITHUB_ENV`` elsewhere.
    """
    if "GITHUB_ENV" not in script:
        return set()
    names: set[str] = set()
    lines = script.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        hm = _HEREDOC_START.search(line)
        if hm:
            delim = hm.group("delim")
            redir_to_env = bool(_ENV_REDIR.search(line))
            body: list[str] = []
            i += 1
            while i < n and lines[i].strip() != delim:
                body.append(lines[i])
                i += 1
            i += 1  # consume the delimiter line
            if redir_to_env:
                for b in body:
                    names |= _env_write_names(b)
            continue

        if stripped == "{":
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith("}"):
                body.append(lines[i])
                i += 1
            close = lines[i] if i < n else ""
            i += 1  # consume the closing-brace line
            if _ENV_REDIR.search(close):
                for b in body:
                    names |= _env_write_names(b)
            continue

        if _ENV_REDIR.search(line):
            names |= _env_write_names(line)
        i += 1
    return names


def _steps_with_env(path: Path) -> tuple[list[dict], dict]:
    """Return ``(steps, base_env)`` for a composite action or reusable workflow.

    ``base_env`` is the workflow-level + job-level ``env:`` that applies to every
    step. Steps from every job of a workflow are returned flattened, each carrying
    its resolved base env under the private key ``__base_env__``.
    """
    doc = yaml.safe_load(path.read_text())
    top_env = dict(doc.get("env") or {})

    runs = doc.get("runs")
    if isinstance(runs, dict) and "steps" in runs:  # composite action
        steps = [dict(s, __base_env__=top_env) for s in runs["steps"]]
        return steps, top_env

    jobs = doc.get("jobs") or {}
    steps: list[dict] = []
    for job in jobs.values():
        job_env = {**top_env, **dict(job.get("env") or {})}
        for s in job.get("steps") or []:
            steps.append(dict(s, __base_env__=job_env))
    return steps, top_env


def undefined_run_vars(path: Path) -> list[tuple[str, str]]:
    """Every ``(step_name, VAR)`` a run step reads before it is defined.

    An empty list means every dereferenced uppercase shell variable is provably
    defined (step/job/workflow env, an earlier ``$GITHUB_ENV`` write, a local
    assignment, a guarding operator, or a GitHub default). Any entry is a
    would-be ``unbound variable`` under ``set -u``.
    """
    steps, _top_env = _steps_with_env(path)
    env_from_earlier: set[str] = set()  # names earlier steps wrote to $GITHUB_ENV
    findings: list[tuple[str, str]] = []

    for step in steps:
        raw = step.get("run")
        base_env = step.get("__base_env__", {})
        if raw is None:
            # A non-run step can still export env (an env: block) or write
            # $GITHUB_ENV, but only run steps carry a script; nothing to write.
            continue
        script = _strip_gha_expressions(raw)
        step_env = set(base_env) | set(step.get("env") or {})
        local = _local_definitions(script)

        # A guarding expansion (the ``${X:-}`` form) is safe only at its own occurrence:
        # ``_bare_references`` already excludes those spots, so a name reaching
        # this loop is a genuine bare read that must be defined some other way.
        for name in sorted(_bare_references(script)):
            if (
                name in step_env
                or name in env_from_earlier
                or name in local
                or _is_default_env(name)
            ):
                continue
            findings.append((step.get("name", "<unnamed>"), name))

        # This step's $GITHUB_ENV writes become available to LATER steps.
        env_from_earlier |= _env_writes(script)

    return findings
