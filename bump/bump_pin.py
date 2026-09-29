#!/usr/bin/env python3
"""THE one place a consumer pin is rewritten — shared by the bump chain.

Every ``bump-*.yml`` caller once carried its own hand-copied regex, and they
drifted from the pyproject they were meant to edit:

* one had NEVER matched — it required a three-component ceiling (``,<2.0.0"``)
  while the pyproject spelled a two-component one (``,<2.0"``), so every release
  silently failed to bump the consumer pin;
* another hardcoded the ceiling in its replacement, so the pin broke the moment
  the ceiling moved (``<2.0.0`` → ``<3.0.0``);
* none could see an exact ``==`` pin, so the automation that removes a temporary
  dev-channel pin was disabled by the temporary dev-channel pin — it worked in
  every case except the one it exists for.

So there is now ONE matcher, config-driven: ``pins.toml`` (``--config``, default
``ci/bump/pins.toml``) owns which file(s) each package is pinned in, and this
code owns only the pin SHAPES (a ``>=A,<B`` range whose ceiling is preserved, an
exact ``==A`` pin whose ceiling is derived, and a bare ``>=A`` floor whose shape
is preserved). A package pinned in more than one file has EVERY file rewritten,
and EVERY occurrence within each file is rewritten — a consumer (mlb-dk) that
declares the same pin twice in one ``pyproject.toml`` (once in
``[project.dependencies]``, once in a ``[dependency-groups]`` group) must bump
both, each occurrence keeping its own shape. An occurrence whose shape is not one
of the three supported forms fails the whole rewrite loudly, naming the line.

Usage (from the workflow):

    python3 ci/bump/bump_pin.py set lib-a 2.19.0
    python3 ci/bump/bump_pin.py --check lib-b

No pyproject argument reaches this code: ``pins.toml`` owns which file each
package is bumped in. A caller that restates that path is precisely what broke
the old inline scripts.

Python 3.12+ stdlib only (``tomllib`` is stdlib from 3.11).
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
import tomllib
from pathlib import Path

#: A `>=A,<B` range — the steady-state shape. The ceiling is CAPTURED and
#: replayed, never rewritten: it tracks the wheel's current major and moves by
#: deliberate PR, not by this bump.
_RANGE = r'"{pkg}>=[0-9][^,"]*,\s*(<[0-9][^"]*)"'

#: An exact `==A` pin — the documented dev-channel form (`==X.Y.Z.devN`), taken
#: deliberately when a consumer must ride an unreleased wheel. There is no
#: ceiling to preserve here, so one is derived (see `_derive_ceiling`).
_EXACT = r'"{pkg}==[0-9][^"]*"'

#: A floor with NO ceiling — `"lib-a>=0.5.0"`. Legal and deliberate: some pins
#: carry no upper bound on purpose. The floor moves and the SHAPE is preserved —
#: deriving a ceiling here would silently narrow a bound nobody chose. Tried
#: last, because `_RANGE`'s prefix also matches the start of a ranged pin.
_FLOOR_ONLY = r'"{pkg}>=[0-9][^,"<]*"'

#: Any PIN-shaped quoted string for the package — the enumerator of occurrences.
#: A pin is the package name, optional extras/whitespace, then a version
#: operator (``==`` ``>=`` ``<=`` ``~=`` ``!=`` ``<`` ``>``) or a ``@`` direct
#: reference. Only strings that reach such a marker are enrolled: an incidental
#: mention that merely starts with the name (``"bh-lake"``, ``"bh-lake client"``)
#: carries no operator and is ignored, not failed. EVERY enrolled match must
#: still resolve to one of the three shapes above; one that does not (extras,
#: whitespace around the operator, ``~=``, a ``@`` url) fails the rewrite (naming
#: what was found) rather than being silently skipped — we never let a real pin
#: through unbumped.
#: The negative lookahead is a name boundary: a bump of ``lib-a`` must not enrol
#: ``"lib-a-engine>=..."`` as an unrecognized occurrence and reject the release.
_ANY = r'"{pkg}(?![A-Za-z0-9._-])\s*(?:\[[^"\]]*\])?\s*(?:==|>=|<=|~=|!=|<|>|@)[^"]*"'

#: The default config path when none is passed.
_DEFAULT_CONFIG = Path("ci/bump/pins.toml")


class ConfigError(Exception):
    """``pins.toml`` is missing or malformed. Names the offender."""


class PinNotFoundError(RuntimeError):
    """No rewritable pin for the package — carries what was found instead."""


class UnknownConsumerError(RuntimeError):
    """The package is not declared in ``pins.toml``."""


class MissingConsumerFileError(RuntimeError):
    """A mapped pin file does not exist on disk."""


#: `--check` outcomes. Distinct codes because the workflow must tell a graceful
#: skip from a red build, and "no pin" and "not a consumer" are different facts
#: even though both mean "nothing to bump right now".
EXIT_NOT_PINNED = 3
EXIT_NOT_A_CONSUMER = 4


def load_config(path: Path) -> dict[str, tuple[str, ...]]:
    """Parse ``pins.toml`` into ``{package: (file, ...)}``.

    A missing/duplicate/empty entry is a loud `ConfigError` naming the offender —
    a stale or ambiguous map is how a bump edits the wrong file (or nothing).
    """
    if not path.exists():
        raise ConfigError(f"pins config: {path} does not exist")
    data = tomllib.loads(path.read_text())
    entries = data.get("package")
    if not entries:
        raise ConfigError(f"pins config: {path} has no [[package]] entries")
    # `[package]` (a single table) instead of `[[package]]` (an array of tables)
    # parses to a dict, not a list. Reject it loudly rather than iterating its
    # keys as if they were package tables (which crashes with a TypeError).
    if not isinstance(entries, list):
        raise ConfigError(
            f"pins config: {path} 'package' must be an array of tables "
            "([[package]]), not a [package] table"
        )
    packages: dict[str, tuple[str, ...]] = {}
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"pins config: [[package]] #{i} must be a table, got "
                f"{type(entry).__name__}"
            )
        if "name" not in entry:
            raise ConfigError(f"pins config: [[package]] #{i} is missing 'name'")
        name = entry["name"]
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"pins config: [[package]] #{i} 'name' must be a non-empty string"
            )
        if "files" not in entry:
            raise ConfigError(f"pins config: package {name!r} is missing 'files'")
        files = entry["files"]
        if not isinstance(files, list) or not files:
            raise ConfigError(f"pins config: package {name!r} 'files' must be a non-empty list")
        if not all(isinstance(f, str) for f in files):
            raise ConfigError(f"pins config: package {name!r} 'files' must all be strings")
        if not all(f for f in files):
            raise ConfigError(
                f"pins config: package {name!r} 'files' must not contain empty strings"
            )
        if name in packages:
            raise ConfigError(f"pins config: duplicate package {name!r}")
        packages[name] = tuple(files)
    return packages


def load_stage_globs(path: Path) -> tuple[str, ...]:
    """Parse the top-level ``stage_globs`` list from ``pins.toml``.

    These are the paths/globs the ``lock_command`` and ``post_lock_command`` are
    allowed to change (the lock file itself, plus any exported outputs) — nothing
    ecosystem-specific is baked into this code, so a project that relocks with
    npm, poetry, uv or anything else just lists the file(s) its command writes.
    Missing/empty is fine (an empty tuple); a non-list or a non-string entry is a
    loud ``ConfigError``.
    """
    if not path.exists():
        raise ConfigError(f"pins config: {path} does not exist")
    data = tomllib.loads(path.read_text())
    globs = data.get("stage_globs", [])
    if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
        raise ConfigError("pins config: stage_globs must be a list of strings")
    if not all(g for g in globs):
        raise ConfigError("pins config: stage_globs must not contain empty strings")
    return tuple(globs)


def package_names(config: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Every declared package — THE allowlist, generated from the config keys.

    A package cannot be a consumer without a declared file, and vice versa: one
    spelling of "is this a consumer", not a separate allowlist that can drift.
    """
    return tuple(sorted(config))


def consumer_files(package: str, config: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """The repo-relative file(s) where ``package``'s pin is bumped."""
    try:
        return config[package]
    except KeyError:
        raise UnknownConsumerError(
            f"{package!r} is not declared in pins.toml, so there is no file to "
            "bump. Add a [[package]] entry — guessing a path is how a bump edits "
            f"the wrong file. Known: {', '.join(sorted(config))}"
        ) from None


def _derive_ceiling(version: str) -> str:
    """Next major after ``version`` — the semver-correct ceiling for a pin that
    has none.

    Only reached from an exact `==` pin. A range pin's ceiling is always
    preserved verbatim, because deriving one there could silently WIDEN or NARROW
    a bound a human chose deliberately.
    """
    major = version.split(".", 1)[0]
    if not major.isdigit():
        raise ValueError(f"cannot derive a ceiling from version {version!r}")
    return f"<{int(major) + 1}.0.0"


def _classify_occurrence(pin: str, package: str, version: str) -> tuple[str, str] | None:
    """Rewrite ONE matched pin string, classified by its own shape.

    ``pin`` is a complete ``"pkg..."`` occurrence (an ``_ANY`` match). Returns
    ``(new_pin_string, description)`` for a range/exact/floor shape, or ``None``
    if it is none of the three — the caller turns that into a loud, line-named
    failure. Each shape is derived EXACTLY as the single-occurrence logic derived
    it before: the range ceiling is preserved verbatim, the exact ceiling is
    derived from the version's major, the floor keeps having no ceiling.
    """
    pkg = re.escape(package)

    # Anchored against the full occurrence (`fullmatch`) so each pin is classified
    # once, by its own text — the floor-only shape can never match inside a range
    # because it is tested against the whole `"pkg..."` string, not a prefix.
    ranged = re.fullmatch(_RANGE.format(pkg=pkg), pin)
    if ranged is not None:
        ceiling = ranged.group(1)
        return (
            f'"{package}>={version},{ceiling}"',
            f"{package}>={version},{ceiling} (ceiling preserved)",
        )

    exact = re.fullmatch(_EXACT.format(pkg=pkg), pin)
    if exact is not None:
        derived = _derive_ceiling(version)
        return (
            f'"{package}>={version},{derived}"',
            f"{package}>={version},{derived} "
            f"(was an exact == pin; ceiling DERIVED from the version's major)",
        )

    floor_only = re.fullmatch(_FLOOR_ONLY.format(pkg=pkg), pin)
    if floor_only is not None:
        return (
            f'"{package}>={version}"',
            f"{package}>={version} (floor-only pin; no ceiling, shape preserved)",
        )

    return None


def rewrite_pin(text: str, package: str, version: str) -> tuple[str, str]:
    """Rewrite EVERY occurrence of ``package``'s pin in ``text`` to a floor of
    ``version``.

    Returns ``(new_text, human_description)``. The description names how many
    occurrences were rewritten and, per occurrence, whether the ceiling was
    PRESERVED, DERIVED or absent — so a reader of the run log can tell each
    apart without opening the diff.

    Every ``_ANY`` occurrence is enumerated and classified once, by position
    (`re.finditer` yields non-overlapping matches, so occurrences never overlap
    or get double-processed). This is a text-level rewrite, not a TOML parse: an
    occurrence inside a comment is rewritten like any other — matching the old
    consumer's `re.subn`-over-the-whole-file behaviour, which is why a consumer
    that pins the package twice bumps both.

    Raises:
        PinNotFoundError: no occurrence at all, OR some occurrence matched none of
            `>=A,<B`, `==A`, `>=A`. Every ``_ANY`` occurrence must be rewritable;
            an unrecognized one fails the whole rewrite (naming it and its line)
            rather than being silently left stale.
    """
    pkg = re.escape(package)

    matches = list(re.finditer(_ANY.format(pkg=pkg), text))
    if not matches:
        raise PinNotFoundError(
            f"no rewritable {package} pin found. Expected a `>=A,<B` range, a bare "
            f"`>=A` floor, or an exact `==A` pin. No {package} pin at all."
        )

    # Classify each occurrence exactly ONCE, keeping its match alongside its
    # outcome. An unsupported shape is collected (with its line) for a loud,
    # single failure; a supported one carries the new pin string and description
    # straight into the rebuild below -- no second classification pass.
    outcomes: list[tuple[re.Match[str], tuple[str, str]]] = []
    unsupported: list[str] = []
    for match in matches:
        outcome = _classify_occurrence(match.group(0), package, version)
        if outcome is None:
            line = text.count("\n", 0, match.start()) + 1
            unsupported.append(f"{match.group(0)} (line {line})")
        else:
            outcomes.append((match, outcome))

    if unsupported:
        raise PinNotFoundError(
            f"no rewritable {package} pin found. Expected a `>=A,<B` range, a bare "
            f"`>=A` floor, or an exact `==A` pin. "
            f"Found instead: {', '.join(unsupported)}"
        )

    # Rebuild the text splicing each occurrence's own new pin in position order.
    parts: list[str] = []
    cursor = 0
    descriptions: list[str] = []
    for match, (new_pin, description) in outcomes:
        parts.append(text[cursor : match.start()])
        parts.append(new_pin)
        cursor = match.end()
        descriptions.append(description)
    parts.append(text[cursor:])
    new_text = "".join(parts)

    count = len(descriptions)
    noun = "occurrence" if count == 1 else "occurrences"
    summary = f"{count} {noun} rewritten: " + "; ".join(descriptions)
    return new_text, summary


def _resolve_file(relative: str, package: str, root: Path) -> Path:
    """Absolute path for a mapped pin file, or a loud `MissingConsumerFileError`."""
    path = root / relative
    if not path.exists():
        raise MissingConsumerFileError(
            f"{relative} does not exist, but pins.toml maps {package!r} to it. "
            "The config is stale, or the consumer file moved."
        )
    return path


def _pinned_elsewhere(
    package: str, root: Path, config: dict[str, tuple[str, ...]], *, excluding: set[str]
) -> list[str]:
    """Mapped files (across all packages, minus ``excluding``) that mention ``package``."""
    candidates = sorted({f for files in config.values() for f in files} - excluding)
    return [
        other
        for other in candidates
        if (root / other).exists()
        and re.search(_ANY.format(pkg=re.escape(package)), (root / other).read_text())
    ]


def rewrite_pin_for_package(
    package: str, version: str, config: dict[str, tuple[str, ...]], *, root: Path = Path(".")
) -> list[tuple[Path, str]]:
    """Bump ``package``'s pin in EVERY declared file, and write each.

    Returns ``[(path_written, human_description), ...]`` — one entry per file.

    On a miss, the OTHER mapped files are searched purely to build the error: a
    package pinned in this repo but not where the bump looked, reported as
    "could not find the pin", is true and indistinguishable from "not a consumer
    of this package". Naming both the path searched and the path where the pin
    actually is turns that into a one-line fix.
    """
    files = consumer_files(package, config)
    own = set(files)
    results: list[tuple[Path, str]] = []
    for relative in files:
        path = _resolve_file(relative, package, root)
        try:
            new_text, description = rewrite_pin(path.read_text(), package, version)
        except PinNotFoundError as exc:
            elsewhere = _pinned_elsewhere(package, root, config, excluding=own)
            if elsewhere:
                raise PinNotFoundError(
                    f"{exc} Searched {relative} (a declared file for {package}), but "
                    f"a {package} pin IS present in: {', '.join(elsewhere)}. Either "
                    "the pin moved or pins.toml is stale — this is not a "
                    "'not a consumer' skip."
                ) from None
            raise PinNotFoundError(f"{exc} Searched {relative}.") from None
        path.write_text(new_text)
        results.append((path, description))
    return results


#: The probe version `--check` rewrites against. Any valid version works and none
#: is written to disk — the question is only whether a REWRITABLE pin exists,
#: which is version-agnostic by construction.
_PROBE_VERSION = "0.0.0"


def _check(package: str, config: dict[str, tuple[str, ...]], root: Path) -> int:
    """Can this repo's declared file(s) for ``package`` be bumped?

    One spelling of "does this repo consume the package": the same map and the
    same matcher the bump itself uses.

    Exit codes are matched as LITERALS by the workflow — YAML cannot import a
    constant, so ``test_exit_codes_are_pinned_literals`` guards them.
    """
    try:
        files = consumer_files(package, config)
    except UnknownConsumerError:
        # NOT an automatic skip. A package this repo demonstrably PINS but never
        # declared is a stale map — say so loudly. Only a package pinned NOWHERE
        # is genuinely not consumed here.
        elsewhere = _pinned_elsewhere(package, root, config, excluding=set())
        if elsewhere:
            print(
                f"::error::{package} is pinned in {', '.join(elsewhere)} but is absent "
                "from pins.toml — the config is stale. Add it (or remove the pin) "
                "rather than letting the release skip a consumer it actually has.",
                file=sys.stderr,
            )
            return 1
        # Routine status is a plain log line, NOT a `::notice::` annotation: the
        # consumer's advance-notice watcher scans annotations for platform
        # deprecations, and would file this steady-state "not a consumer" line as
        # an issue. Annotations are reserved for what a human must act on.
        print(
            f"{package} has no declared consumer here and is pinned nowhere "
            "— not consumed by this repo, nothing to bump."
        )
        return EXIT_NOT_A_CONSUMER

    try:
        rewritable_all = True
        for relative in files:
            path = _resolve_file(relative, package, root)
            try:
                rewrite_pin(path.read_text(), package, _PROBE_VERSION)
            except PinNotFoundError:
                rewritable_all = False
    except MissingConsumerFileError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if not rewritable_all:
        # Plain log line, not a `::notice::` annotation (see _check's not-a-consumer
        # branch): a steady-state "nothing to bump" must not surface as an
        # annotation the advance-notice watcher would file as an issue.
        print(
            f"{', '.join(files)} carries no rewritable {package} pin — nothing to bump."
        )
        return EXIT_NOT_PINNED

    print(f"{package} is pinned in {', '.join(files)} and the pin is rewritable")
    return 0


def _changed_paths(root: Path) -> list[str]:
    """Repo-relative paths ``git status --porcelain`` reports as changed under ``root``.

    Renames are reported as their destination path (the only path that ends up in
    the tree). Untracked and modified alike are returned — a bump that leaves a
    NEW file behind is exactly the kind of stray change this guard exists for.
    """
    out = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    paths: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        entry = line[3:]
        if " -> " in entry:  # rename/copy: XY orig -> dest
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry.strip('"'))
    return paths


def classify_changes(
    changed: list[str], package_files: tuple[str, ...], stage_globs: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    """Split ``changed`` into (allowed, stray).

    Allowed: a declared pin file for the package (always), or a path matching one
    of ``stage_globs`` (the paths the lock/post-lock commands are configured to
    change). Everything else is a stray change that must fail the bump loudly — a
    status-scoped commit is a reviewable release commit, never a blanket sweep of
    whatever the checkout left behind. There is nothing ecosystem-specific here:
    a project that writes ``uv.lock``, ``poetry.lock`` or ``package-lock.json``
    just lists it in ``stage_globs``.
    """
    allowed_exact = set(package_files)
    allowed: list[str] = []
    stray: list[str] = []
    for path in changed:
        if path in allowed_exact or any(fnmatch.fnmatch(path, pattern) for pattern in stage_globs):
            allowed.append(path)
        else:
            stray.append(path)
    return allowed, stray


def _stage(
    package: str,
    config: dict[str, tuple[str, ...]],
    stage_globs: tuple[str, ...],
    root: Path,
) -> int:
    """Stage exactly the files this bump is permitted to touch, or fail loudly.

    The permitted set is the package's declared pin file(s) (always) plus the
    configured ``stage_globs`` — the paths the lock/post-lock commands may write.
    If ``git status`` shows any other changed path, nothing is staged and the
    stray paths are named — a wrong-file bump (or a lock file the project forgot
    to list in ``stage_globs``) is caught here, not discovered in review.
    """
    package_files = consumer_files(package, config)
    changed = _changed_paths(root)
    allowed, stray = classify_changes(changed, package_files, stage_globs)
    if stray:
        allowed_desc = ", ".join(stage_globs) if stage_globs else "(none configured)"
        print(
            f"::error::the {package} bump changed paths it is not allowed to stage: "
            f"{', '.join(sorted(stray))}. Only the declared pin file(s) "
            f"({', '.join(package_files)}) and pins.toml stage_globs "
            f"({allowed_desc}) may change. If the lock/post-lock command wrote one "
            "of these paths, add it to stage_globs.",
            file=sys.stderr,
        )
        return 1
    if allowed:
        subprocess.run(
            ["git", "-C", str(root), "add", "--", *allowed],
            check=True,
            capture_output=True,
            text=True,
        )
    for path in sorted(allowed):
        print(f"staged {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """``set``/``--check``/``stage`` — bump, ask, or stage exactly the bump's files."""
    parser = argparse.ArgumentParser(
        prog="bump_pin",
        description="Rewrite a package's pin in its declared file(s).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the pin exists and is rewritable; write nothing",
    )
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG, help="pins.toml path")
    parser.add_argument("--root", type=Path, default=Path("."), help="repo root (tests)")
    parser.add_argument(
        "tokens",
        nargs="*",
        help="'set <package> <version>', 'stage <package>', or '<package>' (--check)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if args.check:
        if len(args.tokens) != 1:
            parser.error("--check takes exactly one argument: the package name")
        return _check(args.tokens[0], config, args.root)

    if len(args.tokens) == 2 and args.tokens[0] == "stage":
        try:
            stage_globs = load_stage_globs(args.config)
        except ConfigError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            return 1
        try:
            return _stage(args.tokens[1], config, stage_globs, args.root)
        except UnknownConsumerError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            return 1

    if len(args.tokens) != 3 or args.tokens[0] != "set":
        parser.error("expected: set <package> <version>")
    _, package, version = args.tokens

    try:
        written = rewrite_pin_for_package(package, version, config, root=args.root)
    except (
        PinNotFoundError,
        UnknownConsumerError,
        MissingConsumerFileError,
        ValueError,
    ) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    for path, description in written:
        print(f"pinned {description} in {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
