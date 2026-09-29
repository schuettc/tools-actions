"""A static guard that every variable a ``run:`` step (or a ``${{ env.X }}``
expression) dereferences is actually defined before it is read.

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
For every ``run:`` step in an action / reusable workflow it collects, IN ORDER,
every shell variable dereferenced as ``$NAME``, ``${NAME...}``, ``${#NAME}`` or
``${!NAME}`` whose name is UPPERCASE (``[A-Z_][A-Z0-9_]*``), and asserts each
such *bare* reference is satisfied, at the point it is read, by one of:

* that step's ``env:`` (plus any job-level / workflow-level ``env:``);
* an EARLIER step's UNCONDITIONAL write of ``NAME=`` to ``$GITHUB_ENV``;
* a variable assigned or exported EARLIER in the same script (order-aware: a read
  before its assignment is a finding), including ``for`` / ``read`` loop
  variables and the assigning expansions ``${X:=v}`` / ``${X=v}``. An inline
  ``NAME=value cmd`` prefix defines ``NAME`` only for that one command, not for
  the rest of the script;
* GitHub's documented default environment variables (the allowlist below).

It ALSO scans GitHub Actions ``${{ env.X }}`` expressions in a step's ``run:``,
``if:`` and ``with:`` values. An undefined ``env.X`` does not raise — GitHub
resolves it to ``''`` — so it is a silent-default bug rather than a crash, but it
is still reported. An ``env.X`` reference is satisfied by step/job/workflow
``env:`` or ANY earlier ``$GITHUB_ENV`` write (a CONDITIONAL write counts here,
because ``env.X`` degrading to ``''`` is defined behaviour, not a ``set -u``
abort).

A reference that uses a default / alternate / error parameter-expansion operator
— ``${NAME:-x}``, ``${NAME:=x}``, ``${NAME:?x}``, ``${NAME:+x}`` and the
colon-less forms — is *inherently* safe under ``set -u`` (it cannot raise
"unbound variable"), so that occurrence is NOT required to be defined. This is
per-occurrence only. The two assigning forms ``${X:=v}`` and ``${X=v}`` also
DEFINE ``X`` for later bare uses in the same script.

Conditional ``$GITHUB_ENV`` writes
----------------------------------
A ``NAME=`` write to ``$GITHUB_ENV`` INSIDE an ``if`` / ``case`` / ``while`` /
``for`` block is treated as *conditional*: a later step's BARE ``$NAME`` is a
finding (the guarding branch may not have run, so ``$NAME`` can be unbound under
``set -u``), unless that reader guards with ``${NAME:-}``. A conditional write
still satisfies an ``env.NAME`` reference (see above).

Limitations (documented, not silently ignored)
-----------------------------------------------
* The shell tokenizer is regex/line based, not a full shell parser. It splits
  commands on ``;`` ``&&`` ``||`` ``|`` ``&`` and newlines and does not model
  subshell/function *scope*: a variable assigned anywhere earlier in the script
  satisfies a later read, even across function boundaries.
* Conditionality is tracked at LINE granularity by counting ``if``/``case``/
  ``while``/``for`` openers against ``fi``/``esac``/``done`` closers. A
  ``$GITHUB_ENV`` write and its enclosing ``if ...; then ... fi`` written on ONE
  physical line is read as unconditional.
* Only UPPERCASE shell names are checked (the ``unbound variable`` bugs this
  guards against are all screaming-case env vars); a lowercase shell local is not
  tracked. ``${{ env.X }}`` names are matched case-insensitively.
* ``${!NAME}`` is treated as a read of ``NAME`` (the indirection target name is
  not resolved).

The parser is importable so other actions (e.g. ``images``) can adopt the guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# --- GitHub's documented default environment variables ------------------------
# https://docs.github.com/actions/learn-github-actions/variables#default-environment-variables
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
# the colon-less ``${X=v}``).
_ASSIGN_EXPANSION_OPERATORS = (":=", "=")

# ``$NAME`` (no braces): always a bare reference.
_BARE_SIMPLE = re.compile(r"\$([A-Z_][A-Z0-9_]*)")
# ``${NAME...}``: name plus whatever expansion syntax follows.
_BRACED = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)([^}]*)\}")
# ``${#NAME}`` (length) and ``${!NAME}`` (indirection): both READ NAME, and both
# raise under ``set -u`` if NAME is unset.
_BRACED_LEN = re.compile(r"\$\{#([A-Z_][A-Z0-9_]*)\}")
_BRACED_INDIRECT = re.compile(r"\$\{!([A-Z_][A-Z0-9_]*)\}")

# A ``NAME=`` assignment as it appears in an env-write output.
_ENV_ECHO = re.compile(r"""echo\s+["']?([A-Z_][A-Z0-9_]*)=""")
_ENV_PRINT = re.compile(r"""print\(f?["']([A-Z_][A-Z0-9_]*)=""")
_ENV_PLAIN = re.compile(r"""^\s*([A-Z_][A-Z0-9_]*)=""")
# A redirect whose target is exactly ``$GITHUB_ENV`` (``>`` or ``>>``).
_ENV_REDIR = re.compile(r">>?\s*\"?\$\{?GITHUB_ENV\}?(?!\w)")
# A heredoc opener: ``<<WORD`` / ``<<-WORD`` / ``<<'WORD'`` / ``<<"WORD"``.
_HEREDOC_START = re.compile(r"<<-?\s*[\"']?(?P<delim>[A-Za-z_][A-Za-z0-9_]*)[\"']?")
# Strip GitHub Actions ``${{ ... }}`` expressions before scanning a run body so
# they are never mistaken for shell ``${...}`` expansions.
_GHA_EXPR = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
# ``env.X`` reference(s) inside a ``${{ ... }}`` expression (one expression can
# carry several: ``${{ env.A && env.B }}``).
_GHA_ENV_NAME = re.compile(r"\benv\.([A-Za-z_][A-Za-z0-9_]*)")

# Command separators used to split a script into ordered commands.
_CMD_SPLIT = re.compile(r"&&|\|\||[;\n]|\|(?!\|)|&(?!&)")
# A leading assignment at the start of a command (optionally export/local/…).
_LEAD_ASSIGN = re.compile(
    r"^\s*(?:export\s+|local\s+|declare\s+|readonly\s+)?"
    r"([A-Z_][A-Z0-9_]*)(?:\[[^\]]*\])?\+?=(?!=)"
)
_FOR_LOOP = re.compile(r"^\s*for\s+([A-Z_][A-Z0-9_]*)\s+in\b")
_READ = re.compile(r"\bread\b((?:\s+-\w+)*)((?:\s+[A-Za-z_][A-Za-z0-9_]*)+)")

_OPENERS = frozenset({"if", "while", "until", "for", "case"})
_CLOSERS = frozenset({"fi", "done", "esac"})
_LEADING_WORD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")


def _strip_gha_expressions(script: str) -> str:
    return _GHA_EXPR.sub("", script)


def _consume_value(s: str, i: int) -> int:
    """Return the index just past a shell value starting at ``i`` — respecting
    single/double quotes, ``$(...)`` (nested) and backticks, so a value that
    contains spaces (``X=$(cmd a b)``) is consumed whole rather than treated as
    ``X=$(cmd`` plus a trailing command."""
    n = len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            break
        if c == "'":
            i += 1
            while i < n and s[i] != "'":
                i += 1
            i += 1
            continue
        if c == '"':
            i += 1
            while i < n and s[i] != '"':
                if s[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if c == "`":
            i += 1
            while i < n and s[i] != "`":
                i += 1
            i += 1
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            depth = 1
            i += 2
            while i < n and depth > 0:
                d = s[i]
                if d == "(":
                    depth += 1
                elif d == ")":
                    depth -= 1
                elif d == "'":
                    i += 1
                    while i < n and s[i] != "'":
                        i += 1
                elif d == '"':
                    i += 1
                    while i < n and s[i] != '"':
                        i += 1
                i += 1
            continue
        i += 1
    return i


def _all_references(text: str) -> set[str]:
    """Uppercase names dereferenced without a guarding operator (bare reads)."""
    refs: set[str] = set(_BARE_SIMPLE.findall(text))
    refs.update(_BRACED_LEN.findall(text))
    refs.update(_BRACED_INDIRECT.findall(text))
    for name, rest in _BRACED.findall(text):
        if not rest.startswith(_GUARD_OPERATORS):
            refs.add(name)
    return refs


def _defines_from_command(cmd: str) -> tuple[set[str], set[str]]:
    """Return ``(persistent, inline)`` names a single command defines.

    * persistent — an assignment that IS the command (``X=1`` / ``export X=1``),
      a ``for``/``read`` variable, or an assigning ``${X:=v}``: available to the
      rest of the script.
    * inline — a ``NAME=value cmd`` prefix: available only within this command.
    """
    persistent: set[str] = set()
    inline: set[str] = set()

    fm = _FOR_LOOP.match(cmd)
    if fm:
        persistent.add(fm.group(1))
    for _flags, targets in _READ.findall(cmd):
        for tok in targets.split():
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", tok):
                persistent.add(tok)
    for name, rest in _BRACED.findall(cmd):
        if rest.startswith(_ASSIGN_EXPANSION_OPERATORS):
            persistent.add(name)

    # Leading NAME= tokens (possibly several: ``A=1 B=2 cmd``).
    pos = 0
    lead: list[str] = []
    while True:
        m = _LEAD_ASSIGN.match(cmd, pos)
        if not m:
            break
        lead.append(m.group(1))
        pos = _consume_value(cmd, m.end())
        # skip inter-token spaces/tabs
        while pos < len(cmd) and cmd[pos] in " \t":
            pos += 1
    if lead:
        rest = cmd[pos:].strip()
        if rest and rest[0] not in "<>#;&|":
            inline.update(lead)  # a command word follows -> inline env prefix
        else:
            persistent.update(lead)
    return persistent, inline


_FUNC_OPEN = re.compile(r"^[A-Za-z_]\w*\s*\(\)\s*\{")


def _iter_commands(script: str):
    """Yield each command in ``script`` in order, skipping heredoc BODIES (which
    are data, e.g. quoted python, not shell) but keeping opener lines."""
    lines = script.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        hm = _HEREDOC_START.search(line)
        if hm:
            yield from (c for c in _CMD_SPLIT.split(line) if c.strip())
            delim = hm.group("delim")
            i += 1
            while i < n and lines[i].strip() != delim:
                i += 1
            i += 1
            continue
        yield from (c for c in _CMD_SPLIT.split(line) if c.strip())
        i += 1


def _bare_read_findings(script: str, satisfied: set[str]) -> tuple[list[str], set[str]]:
    """Order-aware bare-read scan of one script.

    ``satisfied`` are names defined before the script runs (step/job/workflow
    ``env:``, earlier UNCONDITIONAL ``$GITHUB_ENV`` writes). A read before its
    assignment is a finding, EXCEPT inside a function body: a function is called
    later than it is defined, so its reads are satisfied by any persistent
    assignment anywhere in the script (deferred execution — see Limitations).
    Returns ``(missing, local_defs)``.
    """
    # Pre-pass: every persistent name the script defines anywhere (for function
    # bodies, whose execution is deferred to the call site).
    script_wide: set[str] = set()
    for cmd in _iter_commands(script):
        persistent, _inline = _defines_from_command(cmd)
        script_wide |= persistent

    defined = set(satisfied)
    missing: list[str] = []
    in_function = False
    lines = script.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        hm = _HEREDOC_START.search(line)
        if hm:
            extra = script_wide if in_function else None
            _scan_line_refs(line, defined, missing, extra)
            delim = hm.group("delim")
            i += 1
            while i < n and lines[i].strip() != delim:
                i += 1
            i += 1
            continue
        stripped = line.strip()
        entering = bool(_FUNC_OPEN.match(stripped))
        extra = script_wide if (in_function or entering) else None
        _scan_line_refs(line, defined, missing, extra)
        if entering:
            in_function = True
        elif in_function and stripped == "}":
            in_function = False
        i += 1
    return missing, defined - satisfied


def _scan_line_refs(
    line: str, defined: set[str], missing: list[str], extra: set[str] | None = None
) -> None:
    for cmd in _CMD_SPLIT.split(line):
        if not cmd.strip():
            continue
        persistent, inline = _defines_from_command(cmd)
        available = defined | persistent | inline
        if extra is not None:
            available |= extra
        for name in sorted(_all_references(cmd)):
            if name in available or _is_default_env(name):
                continue
            missing.append(name)
        defined |= persistent


def _env_write_names(text: str) -> set[str]:
    """``NAME=`` assignments carried by a single output line/body line."""
    names: set[str] = set(_ENV_ECHO.findall(text))
    names.update(_ENV_PRINT.findall(text))
    m = _ENV_PLAIN.match(text)
    if m:
        names.add(m.group(1))
    return names


def _leading_word(segment: str) -> str:
    m = _LEADING_WORD.match(segment)
    return m.group(1) if m else ""


def _env_writes_split(script: str) -> tuple[set[str], set[str]]:
    """Names this script writes to ``$GITHUB_ENV``, as ``(all, unconditional)``.

    A ``NAME=`` only counts when the output that carries it is redirected to
    ``$GITHUB_ENV``. A write inside an ``if``/``case``/``while``/``for`` block
    (depth > 0) is conditional and appears in ``all`` but not ``unconditional``.
    Depth is tracked at line granularity (see the module Limitations).
    """
    if "GITHUB_ENV" not in script:
        return set(), set()
    all_writes: set[str] = set()
    uncond: set[str] = set()
    depth = 0
    lines = script.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

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
                    w = _env_write_names(b)
                    all_writes |= w
                    if depth == 0:
                        uncond |= w
            continue

        if line.strip() == "{":
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith("}"):
                body.append(lines[i])
                i += 1
            close = lines[i] if i < n else ""
            i += 1  # consume the closing-brace line
            if _ENV_REDIR.search(close):
                for b in body:
                    w = _env_write_names(b)
                    all_writes |= w
                    if depth == 0:
                        uncond |= w
            continue

        if _ENV_REDIR.search(line):
            w = _env_write_names(line)
            all_writes |= w
            if depth == 0:
                uncond |= w

        # Update nesting depth from this line's structural keywords.
        for seg in line.split(";"):
            word = _leading_word(seg)
            if word in _OPENERS:
                depth += 1
            elif word in _CLOSERS:
                depth = max(0, depth - 1)
        i += 1
    return all_writes, uncond


def _env_writes(script: str) -> set[str]:
    """All names this script writes to ``$GITHUB_ENV`` (conditional or not).

    Kept as the public helper used by other actions and tests; bare-read
    satisfaction uses only the unconditional subset (see :func:`_env_writes_split`).
    """
    return _env_writes_split(script)[0]


def _gha_env_refs(*texts: str) -> set[str]:
    """``env.X`` names referenced in ``${{ ... }}`` expressions across ``texts``."""
    names: set[str] = set()
    for text in texts:
        if not text:
            continue
        for expr in _GHA_EXPR.findall(text):
            names.update(_GHA_ENV_NAME.findall(expr))
    return names


def _with_values(step: dict) -> list[str]:
    out: list[str] = []
    with_block = step.get("with")
    if isinstance(with_block, dict):
        for value in with_block.values():
            if isinstance(value, str):
                out.append(value)
    return out


def _steps_with_env(path: Path) -> tuple[list[dict], dict]:
    """Return ``(steps, base_env)`` for a composite action or reusable workflow."""
    doc = yaml.safe_load(path.read_text())
    top_env = dict(doc.get("env") or {})

    runs = doc.get("runs")
    if isinstance(runs, dict) and "steps" in runs:  # composite action
        steps = [dict(s, __base_env__=top_env) for s in runs["steps"]]
        return steps, top_env

    jobs = doc.get("jobs") or {}
    steps = []
    for job in jobs.values():
        job_env = {**top_env, **dict(job.get("env") or {})}
        for s in job.get("steps") or []:
            steps.append(dict(s, __base_env__=job_env))
    return steps, top_env


def undefined_run_vars(path: Path) -> list[tuple[str, str]]:
    """Every ``(step_name, VAR)`` a step reads before it is defined.

    Covers bare shell reads in ``run:`` (order-aware, under ``set -u``) and
    ``${{ env.X }}`` reads in ``run:``/``if:``/``with:``. An empty list means
    every dereference is provably defined.
    """
    steps, _top_env = _steps_with_env(path)
    uncond_from_earlier: set[str] = set()  # unconditional $GITHUB_ENV writes
    any_from_earlier: set[str] = set()  # every $GITHUB_ENV write (for env.X)
    findings: list[tuple[str, str]] = []

    for step in steps:
        name = step.get("name", "<unnamed>")
        base_env = step.get("__base_env__", {})
        step_env_names = set(step.get("env") or {})
        raw = step.get("run")

        # --- ${{ env.X }} reads in if:/with:/run: ---------------------------
        env_context = set(base_env) | step_env_names | any_from_earlier
        env_context_ci = {n.lower() for n in env_context}
        gha_refs = _gha_env_refs(str(step.get("if", "")), raw or "", *_with_values(step))
        for ref in sorted(gha_refs):
            if ref.lower() in env_context_ci or _is_default_env(ref):
                continue
            findings.append((name, f"env.{ref}"))

        if raw is not None:
            script = _strip_gha_expressions(raw)
            satisfied = set(base_env) | step_env_names | uncond_from_earlier
            missing, _defs = _bare_read_findings(script, satisfied)
            for var in missing:
                findings.append((name, var))

            all_w, uncond_w = _env_writes_split(script)
            uncond_from_earlier |= uncond_w
            any_from_earlier |= all_w

    return findings
