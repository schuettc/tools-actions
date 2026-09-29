"""Tests for the ONE consumer-pin matcher (``bump_pin.py``).

Ported verbatim from muda's ``tests/kits/delivery/test_bump_pin.py``: the
config-driven (``pins.toml``) matcher, plus the multi-file / unknown-shape /
not-in-config / (config-fed) label cases. The module imports ``bump_pin.py``
straight from beside this test (the action directory), so it runs standalone
under any Python 3.12+ with only pytest.

Every case is a real failure this replaces, not a hypothetical: the regexes these
consolidate were each pinned by nothing and drifted for weeks in production.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# bump_pin.py lives beside the action; import it straight from there.
_BUMP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BUMP_DIR))

from bump_pin import (  # noqa: E402
    EXIT_NOT_A_CONSUMER,
    EXIT_NOT_PINNED,
    ConfigError,
    MissingConsumerFileError,
    PinNotFoundError,
    UnknownConsumerError,
    classify_changes,
    consumer_files,
    load_config,
    load_stage_globs,
    main,
    package_names,
    rewrite_pin,
    rewrite_pin_for_package,
)

# The real shapes, verbatim in structure from a consumer pyproject.
A_RANGE = '    "lib-a>=2.17.0,<3.0.0",\n'
C_RANGE = '    "lib-c>=1.0,<2.0",\n'  # a two-component ceiling
A_EXACT = '    "lib-a==2.18.0.dev170",\n'

APP = "packages/app/pyproject.toml"
LIB = "packages/lib/pyproject.toml"


def _config(packages: dict[str, list[str]]) -> dict[str, tuple[str, ...]]:
    return {name: tuple(files) for name, files in packages.items()}


def _write_config(
    tmp_path: Path, packages: dict[str, list[str]], *, stall: str = "chain-stall"
) -> Path:
    lines = [
        'lock_command = ""',
        'post_lock_command = ""',
        f'stall_label = "{stall}"',
    ]
    for name, files in packages.items():
        flist = ", ".join(f'"{f}"' for f in files)
        lines += ["[[package]]", f'name = "{name}"', f"files = [{flist}]"]
    path = tmp_path / "pins.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


# --- pin shapes (text-level, config-free) ------------------------------------


def test_range_pin_moves_the_floor() -> None:
    new, desc = rewrite_pin(A_RANGE, "lib-a", "2.19.0")
    assert '"lib-a>=2.19.0,<3.0.0"' in new
    assert "ceiling preserved" in desc


def test_range_pin_preserves_the_ceiling_it_finds() -> None:
    """A hardcoded `<2.0.0` stopped matching once the ceiling moved to `<3.0.0`.
    The ceiling is captured and replayed, never restated."""
    text = '    "lib-a>=2.17.0,<9.0.0",\n'
    new, _ = rewrite_pin(text, "lib-a", "2.19.0")
    assert '"lib-a>=2.19.0,<9.0.0"' in new


def test_two_component_ceiling_matches() -> None:
    """A matcher demanding `,<2.0.0"` while the pyproject spells `,<2.0"` had
    NEVER matched. A two-component ceiling is legal and must match."""
    new, desc = rewrite_pin(C_RANGE, "lib-c", "1.4.0")
    assert '"lib-c>=1.4.0,<2.0"' in new
    assert "ceiling preserved" in desc


def test_two_component_ceiling_is_not_rewritten_to_three_components() -> None:
    new, _ = rewrite_pin(C_RANGE, "lib-c", "1.4.0")
    assert "<2.0.0" not in new


def test_exact_dev_pin_is_rewritable() -> None:
    """The matcher accepted only a range, so the automation that removes a
    temporary dev pin was disabled BY the temporary dev pin."""
    new, desc = rewrite_pin(A_EXACT, "lib-a", "2.19.0")
    assert '"lib-a>=2.19.0,<3.0.0"' in new
    assert "DERIVED" in desc


def test_exact_pin_ceiling_is_next_major() -> None:
    new, _ = rewrite_pin('    "lib-c==1.2.3.dev9",\n', "lib-c", "1.4.0")
    assert '"lib-c>=1.4.0,<2.0"' not in new
    assert '"lib-c>=1.4.0,<2.0.0"' in new


def test_missing_pin_names_what_it_found() -> None:
    """ "could not find the pin" is true, useless, and forces a log excavation.
    Name the pin found."""
    text = '    "lib-a @ file:///tmp/wheel.whl",\n'
    with pytest.raises(PinNotFoundError, match="Found instead"):
        rewrite_pin(text, "lib-a", "2.19.0")


def test_absent_package_says_so_distinctly() -> None:
    with pytest.raises(PinNotFoundError, match="No lib-a pin at all"):
        rewrite_pin('    "lib-c>=1.0,<2.0",\n', "lib-a", "2.19.0")


def test_every_identical_occurrence_is_rewritten() -> None:
    """A package pinned twice in one file (mlb-dk's `[project.dependencies]` +
    `[dependency-groups]` shape) bumps BOTH — leaving the second stale was the
    bug. Two identical occurrences both move."""
    text = A_RANGE + A_RANGE
    new, desc = rewrite_pin(text, "lib-a", "2.19.0")
    assert new.count('"lib-a>=2.19.0,<3.0.0"') == 2
    assert '"lib-a>=2.17.0,<3.0.0"' not in new
    assert "2 occurrences rewritten" in desc


# The real mlb-dk shape: the same package pinned once in `[project.dependencies]`
# and again in a `[dependency-groups]` group, each occurrence keeping its shape.
MLB_DK_TWO_SHAPES = (
    "[project]\n"
    "dependencies = [\n"
    '    "bh-sport-contract>=0.1.2,<1.0.0",\n'
    "]\n"
    "\n"
    "[dependency-groups]\n"
    "lambda-base = [\n"
    '    "bh-sport-contract>=0.1.2,<1.0.0",\n'
    "]\n"
)


def test_two_occurrences_different_shapes_both_rewrite() -> None:
    """The mlb-dk case with each occurrence a different shape: a range with a
    ceiling in `[project.dependencies]` and an exact `==` pin in the group. Each
    keeps its own derived shape."""
    text = (
        "[project]\n"
        "dependencies = [\n"
        '    "bh-lake>=0.4.0,<1.0.0",\n'
        "]\n"
        "[dependency-groups]\n"
        "lambda-base = [\n"
        '    "bh-lake==0.4.0.dev5",\n'
        "]\n"
    )
    new, desc = rewrite_pin(text, "bh-lake", "0.5.0")
    # Range occurrence: ceiling preserved verbatim.
    assert '"bh-lake>=0.5.0,<1.0.0"' in new
    # Exact occurrence: ceiling DERIVED from the version's major.
    assert '"bh-lake>=0.5.0,<1.0.0"' in new  # derived <1.0.0 for 0.x
    assert '"bh-lake==0.4.0.dev5"' not in new
    assert new.count('"bh-lake>=0.5.0') == 2
    assert "2 occurrences rewritten" in desc
    assert "ceiling preserved" in desc
    assert "DERIVED" in desc


def test_the_real_mlb_dk_dual_range_shape_bumps_both() -> None:
    """The exact mlb-dk pyproject shape from the report: identical range pins in
    `[project.dependencies]` and the `lambda-base` group. Both must move — the
    lambda-base one silently staying at the stale floor was the reported bug."""
    new, desc = rewrite_pin(MLB_DK_TWO_SHAPES, "bh-sport-contract", "0.2.0")
    assert new.count('"bh-sport-contract>=0.2.0,<1.0.0"') == 2
    assert '"bh-sport-contract>=0.1.2,<1.0.0"' not in new
    assert "2 occurrences rewritten" in desc


def test_one_supported_plus_one_unsupported_occurrence_fails(tmp_path: Any) -> None:
    """If ANY occurrence matches none of the supported shapes, the whole rewrite
    fails loudly, naming what it found and the line — the second occurrence must
    never be silently left stale."""
    text = (
        '    "bh-lake>=0.4.0,<1.0.0",\n'
        '    "bh-lake @ file:///tmp/wheel.whl",\n'
    )
    with pytest.raises(PinNotFoundError) as exc:
        rewrite_pin(text, "bh-lake", "0.5.0")
    assert "Found instead" in str(exc.value)
    assert "line 2" in str(exc.value)


def test_an_occurrence_inside_a_comment_is_rewritten() -> None:
    """The matcher is text-level, not a TOML parse — exactly as mlb-dk's old
    `re.subn`-over-the-whole-file logic was. A pin quoted inside a comment is an
    occurrence and is rewritten like any other; documenting the behaviour the old
    code had."""
    text = (
        '    "lib-a>=2.17.0,<3.0.0",\n'
        '    # keep in sync with "lib-a>=2.17.0,<3.0.0" above\n'
    )
    new, desc = rewrite_pin(text, "lib-a", "2.19.0")
    assert new.count('"lib-a>=2.19.0,<3.0.0"') == 2
    assert "2 occurrences rewritten" in desc


def test_unsupported_second_occurrence_via_package_fails_naming_file(tmp_path: Any) -> None:
    """End-to-end through the file layer: a supported first pin and an unsupported
    second occurrence in the SAME mapped file fails, naming the file — closing
    the hole where a second occurrence was silently ignored."""
    (tmp_path / "packages" / "lib").mkdir(parents=True)
    (tmp_path / "packages" / "lib" / "pyproject.toml").write_text(
        '    "lib-b>=0.4.0,<1.0.0",\n    "lib-b @ file:///tmp/wheel.whl",\n'
    )
    config = _config({"lib-b": [LIB]})
    with pytest.raises(PinNotFoundError) as exc:
        rewrite_pin_for_package("lib-b", "0.5.0", config, root=tmp_path)
    assert "Found instead" in str(exc.value)
    assert LIB in str(exc.value)


def test_multi_occurrence_description_counts_them() -> None:
    """The summary names how many occurrences were rewritten — the run log tells a
    single-pin bump from a dual-pin one without opening the diff."""
    single, desc_single = rewrite_pin(A_RANGE, "lib-a", "2.19.0")
    assert "1 occurrence rewritten" in desc_single
    _, desc_double = rewrite_pin(A_RANGE + A_RANGE, "lib-a", "2.19.0")
    assert "2 occurrences rewritten" in desc_double


def test_rewrite_for_package_writes_both_occurrences_in_one_file(tmp_path: Any) -> None:
    """The mlb-dk file layout: both occurrences in one mapped file are written and
    the returned description counts them."""
    (tmp_path / "packages" / "lib").mkdir(parents=True)
    target = tmp_path / "packages" / "lib" / "pyproject.toml"
    target.write_text(MLB_DK_TWO_SHAPES)
    config = _config({"bh-sport-contract": [LIB]})
    written = rewrite_pin_for_package("bh-sport-contract", "0.2.0", config, root=tmp_path)
    assert len(written) == 1
    _, desc = written[0]
    assert target.read_text().count('"bh-sport-contract>=0.2.0,<1.0.0"') == 2
    assert "2 occurrences rewritten" in desc


def test_package_name_is_not_a_regex_injection_point() -> None:
    text = '    "lib-a>=2.17.0,<3.0.0",\n'
    with pytest.raises(PinNotFoundError):
        rewrite_pin(text, "lib.a", "2.19.0")


def test_a_sibling_package_sharing_a_prefix_is_not_matched() -> None:
    """A package must not be rewritten by a bump of a package whose name it
    contains, and vice versa."""
    text = '    "lib-a-engine>=0.1,<0.2",\n    "lib-c>=1.0,<2.0",\n'
    new, _ = rewrite_pin(text, "lib-c", "1.4.0")
    assert '"lib-a-engine>=0.1,<0.2"' in new


def test_non_numeric_version_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="cannot derive a ceiling"):
        rewrite_pin(A_EXACT, "lib-a", "latest")


@pytest.mark.parametrize(
    ("package", "text"),
    [("lib-a", A_RANGE), ("lib-c", C_RANGE)],
)
def test_both_real_workflow_targets_match_today(package: str, text: str) -> None:
    """A matcher nothing exercised is the regression that started this. Both live
    pin shapes must be rewritable by the ONE matcher."""
    version = "9.9.9" if package == "lib-a" else "1.9.9"
    new, _ = rewrite_pin(text, package, version)
    assert f'"{package}>={version},' in new


# --- which file(s) a package is bumped in ------------------------------------

B_RANGE = '    "lib-b>=0.4.0,<1.0.0",\n'
D_FLOOR_ONLY = '    "lib-d>=0.5.0",\n'


def test_packages_resolve_to_their_declared_files() -> None:
    config = _config({"lib-b": [LIB], "lib-d": [LIB], "lib-a": [APP]})
    assert consumer_files("lib-b", config) == (LIB,)
    assert consumer_files("lib-d", config) == (LIB,)
    assert consumer_files("lib-a", config) == (APP,)


def test_pin_package_not_in_config_raises() -> None:
    """A new publishable leaf must be declared deliberately. Guessing a path (or
    scanning for the first hit) is how a bump silently edits the wrong file."""
    config = _config({"lib-a": [APP]})
    with pytest.raises(UnknownConsumerError) as exc:
        consumer_files("lib-x", config)
    assert "lib-x" in str(exc.value)
    assert "pins.toml" in str(exc.value)


def test_the_broker_range_pin_rewrites() -> None:
    new, desc = rewrite_pin(B_RANGE, "lib-b", "0.5.0")
    assert '"lib-b>=0.5.0,<1.0.0"' in new
    assert "ceiling preserved" in desc


def test_a_floor_only_pin_keeps_having_no_ceiling() -> None:
    """A bare floor carries NO ceiling, deliberately. Deriving one here would
    silently narrow a bound nobody chose."""
    new, desc = rewrite_pin(D_FLOOR_ONLY, "lib-d", "0.6.0")
    assert '"lib-d>=0.6.0"' in new
    assert "<" not in new
    assert "no ceiling" in desc


def test_pin_unknown_shape_names_found(tmp_path: Any) -> None:
    """A declared file carrying an unrecognized pin shape fails loudly and names
    what it found, rather than a bare "could not find the pin"."""
    (tmp_path / "packages" / "lib").mkdir(parents=True)
    (tmp_path / "packages" / "lib" / "pyproject.toml").write_text(
        '    "lib-b @ file:///tmp/wheel.whl",\n'
    )
    config = _config({"lib-b": [LIB]})
    with pytest.raises(PinNotFoundError) as exc:
        rewrite_pin_for_package("lib-b", "0.5.0", config, root=tmp_path)
    assert "Found instead" in str(exc.value)
    assert LIB in str(exc.value)


def test_a_pin_in_the_wrong_file_names_both_paths(tmp_path: Any) -> None:
    """The package IS pinned in this repo, just not where the bump looked. The
    error must name what it searched AND where the pin actually is."""
    (tmp_path / "packages" / "lib").mkdir(parents=True)
    (tmp_path / "packages" / "app").mkdir(parents=True)
    # lib-b's pin sits in app (a different package's file) while lib — its
    # declared file — has lost it.
    (tmp_path / "packages" / "lib" / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "packages" / "app" / "pyproject.toml").write_text(B_RANGE)

    config = _config({"lib-b": [LIB], "lib-a": [APP]})
    with pytest.raises(PinNotFoundError) as exc:
        rewrite_pin_for_package("lib-b", "0.5.0", config, root=tmp_path)

    message = str(exc.value)
    assert LIB in message
    assert APP in message
    assert "not a consumer" in message


def test_a_genuinely_unpinned_package_does_not_claim_it_is_elsewhere(tmp_path: Any) -> None:
    """The inverse: no pin anywhere. The error names the path searched and stops."""
    (tmp_path / "packages" / "lib").mkdir(parents=True)
    (tmp_path / "packages" / "app").mkdir(parents=True)
    (tmp_path / "packages" / "lib" / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "packages" / "app" / "pyproject.toml").write_text("[project]\n")

    config = _config({"lib-b": [LIB], "lib-a": [APP]})
    with pytest.raises(PinNotFoundError) as exc:
        rewrite_pin_for_package("lib-b", "0.5.0", config, root=tmp_path)

    assert LIB in str(exc.value)
    assert "IS present in" not in str(exc.value)


def test_rewrite_for_package_writes_the_mapped_file(tmp_path: Any) -> None:
    (tmp_path / "packages" / "lib").mkdir(parents=True)
    target = tmp_path / "packages" / "lib" / "pyproject.toml"
    target.write_text(B_RANGE)

    config = _config({"lib-b": [LIB]})
    written = rewrite_pin_for_package("lib-b", "0.5.0", config, root=tmp_path)

    assert len(written) == 1
    path, desc = written[0]
    assert path == target
    assert '"lib-b>=0.5.0,<1.0.0"' in target.read_text()
    assert "0.5.0" in desc


def test_pin_multi_file_moves_all(tmp_path: Any) -> None:
    """A package pinned in two files has EVERY file rewritten (the mlb shape)."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    f1 = tmp_path / "a" / "pyproject.toml"
    f2 = tmp_path / "b" / "pyproject.toml"
    f1.write_text('    "lib-b>=0.4.0,<1.0.0",\n')
    f2.write_text('    "lib-b>=0.4.0,<1.0.0",\n')

    config = _config({"lib-b": ["a/pyproject.toml", "b/pyproject.toml"]})
    written = rewrite_pin_for_package("lib-b", "0.5.0", config, root=tmp_path)

    assert len(written) == 2
    assert '"lib-b>=0.5.0,<1.0.0"' in f1.read_text()
    assert '"lib-b>=0.5.0,<1.0.0"' in f2.read_text()


# --- one spelling of "is this a consumer" ------------------------------------


def test_a_missing_mapped_file_is_its_own_loud_error(tmp_path: Any) -> None:
    """A missing mapped file means the config is stale or the package moved; say
    so, rather than folding it into "No pin at all. Searched <file>"."""
    (tmp_path / "packages" / "lib").mkdir(parents=True)  # dir exists, file does not
    config = _config({"lib-b": [LIB]})

    with pytest.raises(MissingConsumerFileError) as exc:
        rewrite_pin_for_package("lib-b", "0.5.0", config, root=tmp_path)

    assert LIB in str(exc.value)
    assert "does not exist" in str(exc.value)


def test_check_reports_a_mapped_pin_as_bumpable(tmp_path: Any) -> None:
    (tmp_path / "packages" / "lib").mkdir(parents=True)
    (tmp_path / "packages" / "lib" / "pyproject.toml").write_text(B_RANGE)
    config = _write_config(tmp_path, {"lib-b": [LIB]})

    assert main(["--check", "lib-b", "--root", str(tmp_path), "--config", str(config)]) == 0


def test_check_reports_a_mapped_file_without_the_pin_as_a_skip(tmp_path: Any) -> None:
    """Mapped, file present, no pin: a genuine "not a consumer of this package" —
    a graceful skip, NOT a red build."""
    (tmp_path / "packages" / "lib").mkdir(parents=True)
    (tmp_path / "packages" / "lib" / "pyproject.toml").write_text("[project]\n")
    config = _write_config(tmp_path, {"lib-b": [LIB]})

    assert (
        main(["--check", "lib-b", "--root", str(tmp_path), "--config", str(config)])
        == EXIT_NOT_PINNED
    )


def test_check_reports_an_unmapped_package_as_a_skip(tmp_path: Any) -> None:
    """A package in no config and pinned nowhere is simply not consumed here."""
    config = _write_config(tmp_path, {"lib-b": [LIB]})
    assert (
        main(["--check", "lib-x", "--root", str(tmp_path), "--config", str(config)])
        == EXIT_NOT_A_CONSUMER
    )


def test_check_reports_a_missing_mapped_file_as_a_hard_failure(tmp_path: Any) -> None:
    """The one case that must stay RED: mapped, but the file is gone."""
    config = _write_config(tmp_path, {"lib-b": [LIB]})
    assert main(["--check", "lib-b", "--root", str(tmp_path), "--config", str(config)]) == 1


def test_the_allowlist_is_derived_from_the_map() -> None:
    """Whatever the allowlist becomes, it is generated from the config keys — so a
    package cannot be allowlisted without a declared file ever again."""
    config = _config({"lib-b": [LIB], "lib-a": [APP]})
    assert set(package_names(config)) == set(config)
    assert "lib-x" not in package_names(config)


def test_exit_codes_are_pinned_literals() -> None:
    """The workflow matches these NUMBERS in a `case` — YAML cannot import a
    Python constant, so renumbering must fail in CI, not mid-release."""
    assert EXIT_NOT_PINNED == 3
    assert EXIT_NOT_A_CONSUMER == 4


def test_check_goes_red_when_a_pinned_package_is_absent_from_the_config(tmp_path: Any) -> None:
    """Config absence alone was once a SILENT GREEN skip for a package this repo
    demonstrably pins. A pin that exists with no config entry means the config is
    stale, and the release must stop."""
    (tmp_path / "packages" / "app").mkdir(parents=True)
    (tmp_path / "packages" / "app" / "pyproject.toml").write_text(
        '    "lib-new-leaf>=1.0.0,<2.0.0",\n'
    )
    config = _write_config(tmp_path, {"lib-a": [APP]})

    assert main(["--check", "lib-new-leaf", "--root", str(tmp_path), "--config", str(config)]) == 1


def test_set_writes_the_pin(tmp_path: Any) -> None:
    (tmp_path / "packages" / "lib").mkdir(parents=True)
    target = tmp_path / "packages" / "lib" / "pyproject.toml"
    target.write_text(B_RANGE)
    config = _write_config(tmp_path, {"lib-b": [LIB]})

    assert main(["set", "lib-b", "0.5.0", "--root", str(tmp_path), "--config", str(config)]) == 0
    assert '"lib-b>=0.5.0,<1.0.0"' in target.read_text()


def test_set_unknown_package_is_a_failure(tmp_path: Any) -> None:
    config = _write_config(tmp_path, {"lib-b": [LIB]})
    assert main(["set", "lib-x", "0.5.0", "--root", str(tmp_path), "--config", str(config)]) == 1


# --- scoped staging (the `git add -A` fix) -----------------------------------


def _write_config_with_globs(
    tmp_path: Path, packages: dict[str, list[str]], stage_globs: list[str]
) -> Path:
    lines = [
        'lock_command = ""',
        'post_lock_command = ""',
        'stall_label = "chain-stall"',
        "stage_globs = [" + ", ".join(f'"{g}"' for g in stage_globs) + "]",
    ]
    for name, files in packages.items():
        flist = ", ".join(f'"{f}"' for f in files)
        lines += ["[[package]]", f'name = "{name}"', f"files = [{flist}]"]
    path = tmp_path / "pins.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)


def _staged(root: Path) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {line for line in out.splitlines() if line}


def test_classify_changes_splits_allowed_from_stray() -> None:
    """Pinned files are ALWAYS allowed; the lock and post-lock outputs are allowed
    only because they are listed in stage_globs — nothing is hardcoded."""
    allowed, stray = classify_changes(
        [
            "pyproject.toml",
            "uv.lock",
            "infra/docker/requirements-app.lock",
            "src/rogue.py",
        ],
        ("pyproject.toml",),
        ("uv.lock", "infra/docker/requirements-*.lock"),
    )
    assert allowed == ["pyproject.toml", "uv.lock", "infra/docker/requirements-app.lock"]
    assert stray == ["src/rogue.py"]


def test_classify_changes_a_non_uv_lock_is_staged_when_listed() -> None:
    """Nothing is uv-specific: a project whose lock command writes package-lock.json
    lists it in stage_globs and it is staged like any other allowed path."""
    allowed, stray = classify_changes(
        ["package.json", "package-lock.json"],
        ("package.json",),
        ("package-lock.json",),
    )
    assert allowed == ["package.json", "package-lock.json"]
    assert stray == []


def test_classify_changes_a_lock_absent_from_stage_globs_is_stray() -> None:
    """With no stage_globs, a changed lock file is a stray path (misconfiguration
    made visible), NOT a silently-allowed sweep by a hardcoded lock basename."""
    allowed, stray = classify_changes(
        ["pyproject.toml", "uv.lock"],
        ("pyproject.toml",),
        (),
    )
    assert allowed == ["pyproject.toml"]
    assert stray == ["uv.lock"]


def test_stage_fails_loudly_on_a_stray_path(tmp_path: Any, capsys: Any) -> None:
    """A change outside the pinned file(s)/stage_globs is a stray change: the stage
    step fails and names it, rather than sweeping it into the release commit via
    `git add -A`."""
    root = tmp_path / "repo"
    (root / "packages" / "lib").mkdir(parents=True)
    (root / "packages" / "lib" / "pyproject.toml").write_text(B_RANGE)
    config = _write_config_with_globs(root, {"lib-b": [LIB]}, [])
    _init_repo(root)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)

    # An allowed change (the pin) AND a stray change (an unrelated file).
    (root / "packages" / "lib" / "pyproject.toml").write_text('    "lib-b>=0.5.0,<1.0.0",\n')
    (root / "rogue.txt").write_text("unexpected\n")

    rc = main(["stage", "lib-b", "--root", str(root), "--config", str(config)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "rogue.txt" in err
    assert "not allowed to stage" in err
    # Nothing staged on failure.
    assert _staged(root) == set()


def test_stage_stages_pin_lock_and_export_outputs(tmp_path: Any) -> None:
    """Only the pinned file(s) plus stage_globs matches (here uv.lock and the
    requirements exports) are staged — exactly what a bump+relock+export touches."""
    root = tmp_path / "repo"
    (root / "packages" / "lib").mkdir(parents=True)
    (root / "infra" / "docker").mkdir(parents=True)
    (root / "packages" / "lib" / "pyproject.toml").write_text(B_RANGE)
    (root / "uv.lock").write_text("# lock\n")
    (root / "infra" / "docker" / "requirements-app.lock").write_text("old\n")
    config = _write_config_with_globs(
        root, {"lib-b": [LIB]}, ["uv.lock", "infra/docker/requirements-*.lock"]
    )
    _init_repo(root)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)

    (root / "packages" / "lib" / "pyproject.toml").write_text('    "lib-b>=0.5.0,<1.0.0",\n')
    (root / "uv.lock").write_text("# relocked\n")
    (root / "infra" / "docker" / "requirements-app.lock").write_text("new\n")

    rc = main(["stage", "lib-b", "--root", str(root), "--config", str(config)])
    assert rc == 0
    assert _staged(root) == {
        LIB,
        "uv.lock",
        "infra/docker/requirements-app.lock",
    }


def test_stage_stages_a_non_uv_lock_file(tmp_path: Any) -> None:
    """A project whose lock_command writes package-lock.json lists it in stage_globs
    and the stage step commits it — the code carries no uv assumption."""
    root = tmp_path / "repo"
    (root / "packages" / "lib").mkdir(parents=True)
    (root / "packages" / "lib" / "pyproject.toml").write_text(B_RANGE)
    (root / "package-lock.json").write_text('{"v": 1}\n')
    config = _write_config_with_globs(root, {"lib-b": [LIB]}, ["package-lock.json"])
    _init_repo(root)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)

    (root / "packages" / "lib" / "pyproject.toml").write_text('    "lib-b>=0.5.0,<1.0.0",\n')
    (root / "package-lock.json").write_text('{"v": 2}\n')

    rc = main(["stage", "lib-b", "--root", str(root), "--config", str(config)])
    assert rc == 0
    assert _staged(root) == {LIB, "package-lock.json"}


def test_stage_fails_loudly_naming_a_lock_absent_from_stage_globs(
    tmp_path: Any, capsys: Any
) -> None:
    """A lock_command that writes a lock NOT listed in stage_globs is a
    misconfiguration: the stage step fails and NAMES the lock, so the missing
    stage_globs entry is visible rather than the lock being swept in silently."""
    root = tmp_path / "repo"
    (root / "packages" / "lib").mkdir(parents=True)
    (root / "packages" / "lib" / "pyproject.toml").write_text(B_RANGE)
    (root / "uv.lock").write_text("# lock\n")
    # lock_command is set in config, but stage_globs is EMPTY — the lock is stray.
    config = _write_config_with_globs(root, {"lib-b": [LIB]}, [])
    _init_repo(root)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)

    (root / "packages" / "lib" / "pyproject.toml").write_text('    "lib-b>=0.5.0,<1.0.0",\n')
    (root / "uv.lock").write_text("# relocked\n")

    rc = main(["stage", "lib-b", "--root", str(root), "--config", str(config)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "uv.lock" in err
    assert "stage_globs" in err
    # Nothing staged on failure.
    assert _staged(root) == set()


# --- routine status is a plain log line, never a `::notice::` annotation ------
# Deliberate tools-actions deviation from the muda source: the consumer's
# advance-notice watcher scans GitHub annotations for platform deprecations and
# was filing steady-state bump status ("not a consumer", "nothing to bump") as
# issues. Annotations are reserved for what a human must act on, so routine
# status is routed to plain log lines. `::error::`/`::warning::` stay for real
# failures and the stall path.


def test_check_not_a_consumer_emits_no_notice_annotation(tmp_path: Any, capsys: Any) -> None:
    """An unmapped package pinned nowhere is a graceful skip — its status must be a
    plain log line, not a `::notice::` the watcher would file as an issue."""
    config = _write_config(tmp_path, {"lib-b": [LIB]})
    assert (
        main(["--check", "lib-x", "--root", str(tmp_path), "--config", str(config)])
        == EXIT_NOT_A_CONSUMER
    )
    captured = capsys.readouterr()
    assert "::notice::" not in captured.out
    assert "::notice::" not in captured.err


def test_check_nothing_to_bump_emits_no_notice_annotation(tmp_path: Any, capsys: Any) -> None:
    """A mapped file present but carrying no pin is a graceful skip — plain log,
    no `::notice::` annotation."""
    (tmp_path / "packages" / "lib").mkdir(parents=True)
    (tmp_path / "packages" / "lib" / "pyproject.toml").write_text("[project]\n")
    config = _write_config(tmp_path, {"lib-b": [LIB]})
    assert (
        main(["--check", "lib-b", "--root", str(tmp_path), "--config", str(config)])
        == EXIT_NOT_PINNED
    )
    captured = capsys.readouterr()
    assert "::notice::" not in captured.out
    assert "::notice::" not in captured.err


def test_check_success_emits_no_notice_annotation(tmp_path: Any, capsys: Any) -> None:
    """The success path (pinned + rewritable) prints plain status, never a
    `::notice::`."""
    (tmp_path / "packages" / "lib").mkdir(parents=True)
    (tmp_path / "packages" / "lib" / "pyproject.toml").write_text(B_RANGE)
    config = _write_config(tmp_path, {"lib-b": [LIB]})
    assert main(["--check", "lib-b", "--root", str(tmp_path), "--config", str(config)]) == 0
    captured = capsys.readouterr()
    assert "::notice::" not in captured.out
    assert "::notice::" not in captured.err


# --- config validation: one dedicated test per load_config/load_stage_globs branch
# tools-actions ships no copier layer, so these rules are enforced ONLY at runtime.
# Each test asserts the ConfigError message NAMES the problem, and the empty-string
# cases guard the images-I1 class of bug (an empty value slipping through).


def _toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pins.toml"
    path.write_text(body)
    return path


def test_config_missing_file_names_it(tmp_path: Path) -> None:
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError) as exc:
        load_config(missing)
    assert "does not exist" in str(exc.value) and "nope.toml" in str(exc.value)


def test_config_empty_package_table_is_named(tmp_path: Path) -> None:
    config = _toml(tmp_path, 'stall_label = "chain-stall"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "[[package]]" in str(exc.value)


def test_config_package_missing_name_is_named(tmp_path: Path) -> None:
    config = _toml(tmp_path, "[[package]]\nfiles = [\"pyproject.toml\"]\n")
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "name" in str(exc.value)


def test_config_package_empty_name_is_named(tmp_path: Path) -> None:
    config = _toml(tmp_path, '[[package]]\nname = ""\nfiles = ["pyproject.toml"]\n')
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "name" in str(exc.value) and "non-empty" in str(exc.value)


def test_config_package_missing_files_is_named(tmp_path: Path) -> None:
    config = _toml(tmp_path, '[[package]]\nname = "lib-a"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "lib-a" in str(exc.value) and "files" in str(exc.value)


def test_config_package_empty_files_list_is_named(tmp_path: Path) -> None:
    config = _toml(tmp_path, '[[package]]\nname = "lib-a"\nfiles = []\n')
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "files" in str(exc.value) and "non-empty" in str(exc.value)


def test_config_package_non_list_files_is_named(tmp_path: Path) -> None:
    config = _toml(tmp_path, '[[package]]\nname = "lib-a"\nfiles = "pyproject.toml"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "files" in str(exc.value)


def test_config_package_non_string_files_entry_is_named(tmp_path: Path) -> None:
    config = _toml(tmp_path, '[[package]]\nname = "lib-a"\nfiles = [123]\n')
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "files" in str(exc.value) and "string" in str(exc.value)


def test_config_package_empty_string_file_is_named(tmp_path: Path) -> None:
    """An empty-string filename is the images-I1 empty-value bug class: it must be
    rejected loudly, not silently accepted as a valid path."""
    config = _toml(tmp_path, '[[package]]\nname = "lib-a"\nfiles = [""]\n')
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "files" in str(exc.value) and "empty" in str(exc.value)


def test_config_duplicate_package_is_named(tmp_path: Path) -> None:
    config = _toml(
        tmp_path,
        '[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n'
        '[[package]]\nname = "lib-a"\nfiles = ["b.toml"]\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "duplicate" in str(exc.value) and "lib-a" in str(exc.value)


def test_config_valid_round_trips(tmp_path: Path) -> None:
    config = _toml(
        tmp_path,
        '[[package]]\nname = "lib-a"\nfiles = ["a.toml", "b.toml"]\n',
    )
    assert load_config(config) == {"lib-a": ("a.toml", "b.toml")}


# --- stage_globs branches ----------------------------------------------------


def test_stage_globs_non_list_is_named(tmp_path: Path) -> None:
    config = _toml(
        tmp_path,
        'stage_globs = "uv.lock"\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_stage_globs(config)
    assert "stage_globs" in str(exc.value)


def test_stage_globs_non_string_entry_is_named(tmp_path: Path) -> None:
    config = _toml(
        tmp_path,
        'stage_globs = [1]\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_stage_globs(config)
    assert "stage_globs" in str(exc.value) and "string" in str(exc.value)


def test_stage_globs_empty_string_entry_is_named(tmp_path: Path) -> None:
    """An empty-string glob is the images-I1 empty-value class: rejected, not
    silently treated as 'match nothing' (or worse, everything)."""
    config = _toml(
        tmp_path,
        'stage_globs = [""]\n[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_stage_globs(config)
    assert "stage_globs" in str(exc.value) and "empty" in str(exc.value)


def test_stage_globs_absent_is_empty_tuple(tmp_path: Path) -> None:
    config = _toml(tmp_path, '[[package]]\nname = "lib-a"\nfiles = ["a.toml"]\n')
    assert load_stage_globs(config) == ()


def test_config_package_table_not_array_raises_configerror(tmp_path: Path) -> None:
    """A `[package]` TABLE instead of a `[[package]]` array-of-tables must raise a
    ConfigError that names the fix — not crash with a bare TypeError (M5)."""
    config = _toml(tmp_path, '[package]\nname = "lib-a"\nfiles = ["a.toml"]\n')
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "[[package]]" in str(exc.value)


def test_config_package_entry_not_a_table_raises_configerror(tmp_path: Path) -> None:
    """An array element that is not a table (e.g. a bare string) is named, not a
    TypeError."""
    config = _toml(tmp_path, 'package = ["lib-a"]\n')
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "table" in str(exc.value)
