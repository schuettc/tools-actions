#!/usr/bin/env python3
"""Verify the toolchain is pinned once and agrees with itself, repo-wide.

A repo pins its Python and Node once — in ``.python-version`` and ``.nvmrc`` —
and every other place that names a version (a Dockerfile ``FROM``, a
``setup-python``/``setup-node`` workflow step) must defer to those files rather
than restate a version that then drifts. This check reads every Dockerfile and
workflow and reports every place that disagrees, so a stale base image or a
hand-written ``python-version:`` cannot merge.

It uses the standard library only (``re`` + ``pathlib``); workflow YAML is read
line-based, not parsed, so no third-party dependency is required.

Findings (every one is printed; the check never stops at the first):

* a Dockerfile base Python ``X.Y`` that differs from ``.python-version``'s
  ``X.Y``;
* a base image that is not pinned by digest (``@sha256:``), or that is pinned by
  digest but is not documented by a **tag comment**;
* an ``ARG``-substituted ``FROM`` that cannot be resolved (it must fail loudly as
  ``unresolvable``, never silently pass);
* a ``setup-python`` step that lacks ``python-version-file: .python-version`` or
  that carries a ``python-version:`` key;
* when ``.nvmrc`` exists, a ``setup-node`` step that lacks
  ``node-version-file: .nvmrc``;
* a missing ``.python-version``.

**Tag-comment rule.** The platform's digest-pinned lake-plane base is written as
``FROM public.ecr.aws/lambda/python:3.14@sha256:...`` with a comment block above
it that documents the tag it was resolved from — the refresh command it carries,
``docker buildx imagetools inspect public.ecr.aws/lambda/python:3.14``, names the
``image:tag`` in a comment line. Generalised: a digest-pinned base is accepted
only when one of the contiguous comment lines immediately above the ``FROM``
contains that image's ``path:tag`` (the image path followed by a version tag).
That comment is also where the Python ``X.Y`` is read from when the ``FROM`` line
itself carries only a digest and no tag.

Which files:

* Dockerfiles — any file named ``Dockerfile``, ``Dockerfile.*`` or
  ``*.Dockerfile`` under the repo, excluding ``.git``, ``node_modules``,
  ``.venv``, ``cdk.out`` and ``.worktrees``. ``docker-compose`` files are not
  Dockerfiles and are ignored.
* Workflows — ``.github/workflows/*.yml``/``*.yaml`` and
  ``.github/actions/**/action.yml``.

Exit 1 if there is any finding, 0 when clean.

    python3 ci/checks/toolchain_consistency.py [--root .]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Directory names never descended into when discovering Dockerfiles.
_PRUNE_DIRS = frozenset({".git", "node_modules", ".venv", "cdk.out", ".worktrees"})

# A base image is a Python image when its path is exactly ``python`` or ends in
# ``/python`` (which also covers ``.../library/python``).
_PY_PATH_RE = re.compile(r"(^|/)python$")

# Leading ``X.Y`` of a version tag: ``3.14``, ``3.14.7``, ``3.12-slim-bookworm``.
_XY_RE = re.compile(r"^(\d+)\.(\d+)")

# A ``FROM`` line, capturing an optional ``--platform`` flag, the image ref and an
# optional stage alias (``AS name``).
_FROM_RE = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)(?:\s+AS\s+(?P<alias>\S+))?\s*$",
    re.IGNORECASE,
)


class Finding:
    """One reported disagreement: a file, an optional line and a message."""

    def __init__(self, path: str, message: str, line: int | None = None) -> None:
        self.path = path
        self.message = message
        self.line = line

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{where}: {self.message}"


def _read_python_xy(root: Path) -> tuple[str | None, list[Finding]]:
    """Return the ``X.Y`` from ``.python-version`` (or a missing-file finding)."""
    path = root / ".python-version"
    if not path.exists():
        return None, [Finding(".python-version", "missing (the repo must pin Python here)")]
    match = _XY_RE.match(path.read_text().strip())
    if match is None:
        return None, [Finding(".python-version", "does not start with a X.Y version")]
    return f"{match.group(1)}.{match.group(2)}", []


def _parse_image_ref(ref: str) -> tuple[str, str | None, str | None]:
    """Split an image ref into ``(path, tag, digest)``.

    The tag is the ``:`` that follows the final ``/`` (so a registry ``host:port``
    is not mistaken for a tag). ``@sha256:...`` is the digest.
    """
    digest: str | None = None
    base = ref
    if "@" in ref:
        base, digest = ref.split("@", 1)
    slash = base.rfind("/")
    colon = base.find(":", slash + 1)
    if colon != -1:
        return base[:colon], base[colon + 1 :], digest
    return base, None, digest


def _tag_comment_present(comment_lines: list[str], path: str) -> str | None:
    """Return the tag documented for ``path`` in the comment block, or ``None``.

    The comment block is the contiguous run of ``#`` lines immediately above the
    ``FROM``. A line documents the tag when it contains ``<path>:<tag>``.
    """
    pattern = re.compile(re.escape(path) + r":([\w.\-]+)")
    for line in comment_lines:
        match = pattern.search(line)
        if match is not None:
            return match.group(1)
    return None


def _check_dockerfile(path: Path, rel: str, python_xy: str | None) -> list[Finding]:
    """Report every toolchain disagreement in a single Dockerfile."""
    findings: list[Finding] = []
    lines = path.read_text().splitlines()
    aliases: set[str] = set()
    for index, raw in enumerate(lines):
        match = _FROM_RE.match(raw)
        if match is None:
            continue
        lineno = index + 1
        image = match.group("image")
        alias = match.group("alias")

        # A stage-alias FROM (``FROM base AS light``) and ``FROM scratch`` are not
        # base images; they are never checked for pinning or Python version.
        lowered = image.lower()
        if lowered == "scratch" or lowered in aliases:
            if alias is not None:
                aliases.add(alias.lower())
            continue

        # An ARG-substituted FROM cannot be resolved statically. Fail loudly.
        if "$" in image:
            findings.append(
                Finding(rel, f"FROM uses an unresolvable ARG substitution: {image}", lineno)
            )
            if alias is not None:
                aliases.add(alias.lower())
            continue

        if alias is not None:
            aliases.add(alias.lower())

        img_path, tag, digest = _parse_image_ref(image)

        # The contiguous comment block immediately above this FROM.
        comment_lines: list[str] = []
        back = index - 1
        while back >= 0 and lines[back].lstrip().startswith("#"):
            comment_lines.append(lines[back])
            back -= 1

        # Digest pinning applies to every real base image (Python or not).
        if digest is None:
            findings.append(
                Finding(rel, f"base image is not pinned by digest (@sha256:): {image}", lineno)
            )
        elif _tag_comment_present(comment_lines, img_path) is None:
            findings.append(
                Finding(
                    rel,
                    "digest-pinned base image lacks a tag comment "
                    f"(a '#' line above naming {img_path}:<tag>): {image}",
                    lineno,
                )
            )

        # Python version comparison applies only to Python base images.
        if not _PY_PATH_RE.search(img_path):
            continue
        version_tag = tag
        if version_tag is None:
            version_tag = _tag_comment_present(comment_lines, img_path)
        if version_tag is None:
            findings.append(
                Finding(rel, f"Python base image has no discernible version tag: {image}", lineno)
            )
            continue
        xy_match = _XY_RE.match(version_tag)
        if xy_match is None:
            findings.append(
                Finding(rel, f"Python base image tag has no X.Y version: {version_tag}", lineno)
            )
            continue
        base_xy = f"{xy_match.group(1)}.{xy_match.group(2)}"
        if python_xy is not None and base_xy != python_xy:
            findings.append(
                Finding(
                    rel,
                    f"Python base image {base_xy} differs from .python-version {python_xy}",
                    lineno,
                )
            )
    return findings


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _value(line: str) -> str:
    """The scalar value after ``key:``, stripped of quotes and whitespace."""
    value = line.split(":", 1)[1].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _step_block(lines: list[str], start: int) -> list[str]:
    """The lines belonging to the step whose ``uses:`` is at ``lines[start]``.

    The block runs forward until a new list item (``- ``) at or below the step's
    indentation, or a dedent below it — enough to see the step's ``with:`` keys
    without a YAML parser.
    """
    indent = _leading_spaces(lines[start])
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.strip():
            block.append(line)
            continue
        lead = _leading_spaces(line)
        stripped = line.lstrip(" ")
        if stripped.startswith("- ") and lead <= indent:
            break
        if lead < indent:
            break
        block.append(line)
    return block


def _find_key(block: list[str], key: str) -> str | None:
    """The value of the first ``key:`` line in the block, or ``None``."""
    prefix = re.compile(r"^\s*" + re.escape(key) + r"\s*:")
    for line in block:
        if prefix.match(line):
            return _value(line)
    return None


def _has_key(block: list[str], key: str) -> bool:
    prefix = re.compile(r"^\s*" + re.escape(key) + r"\s*:")
    return any(prefix.match(line) for line in block)


def _check_workflow(path: Path, rel: str, has_nvmrc: bool) -> list[Finding]:
    """Report every setup-python / setup-node disagreement in one workflow file."""
    findings: list[Finding] = []
    lines = path.read_text().splitlines()
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped.startswith("- uses:") and not stripped.startswith("uses:"):
            continue
        lineno = index + 1
        if "setup-python@" in raw:
            block = _step_block(lines, index)
            if _has_key(block, "python-version"):
                findings.append(
                    Finding(
                        rel,
                        "setup-python must not carry 'python-version:'; "
                        "use 'python-version-file: .python-version'",
                        lineno,
                    )
                )
            if _find_key(block, "python-version-file") != ".python-version":
                findings.append(
                    Finding(
                        rel,
                        "setup-python must set 'python-version-file: .python-version'",
                        lineno,
                    )
                )
        elif "setup-node@" in raw and has_nvmrc:
            block = _step_block(lines, index)
            if _find_key(block, "node-version-file") != ".nvmrc":
                findings.append(
                    Finding(
                        rel,
                        "setup-node must set 'node-version-file: .nvmrc'",
                        lineno,
                    )
                )
    return findings


def _iter_dockerfiles(root: Path) -> list[Path]:
    """Every Dockerfile under ``root`` (pruning vendored/output dirs)."""
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in _PRUNE_DIRS for part in rel_parts[:-1]):
            continue
        name = path.name
        if name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".Dockerfile"):
            found.append(path)
    return found


def _iter_workflows(root: Path) -> list[Path]:
    """Every workflow and composite-action file under ``.github``."""
    found: list[Path] = []
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        for path in sorted(workflows.iterdir()):
            if path.is_file() and path.suffix in {".yml", ".yaml"}:
                found.append(path)
    actions = root / ".github" / "actions"
    if actions.is_dir():
        for path in sorted(actions.rglob("action.yml")):
            if path.is_file():
                found.append(path)
    return found


def check(root: Path) -> list[Finding]:
    """Return every toolchain-consistency finding under ``root``."""
    findings: list[Finding] = []
    python_xy, version_findings = _read_python_xy(root)
    findings.extend(version_findings)

    for dockerfile in _iter_dockerfiles(root):
        rel = str(dockerfile.relative_to(root))
        findings.extend(_check_dockerfile(dockerfile, rel, python_xy))

    has_nvmrc = (root / ".nvmrc").exists()
    for workflow in _iter_workflows(root):
        rel = str(workflow.relative_to(root))
        findings.extend(_check_workflow(workflow, rel, has_nvmrc))

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="toolchain_consistency",
        description="Verify Dockerfiles and workflows agree with .python-version/.nvmrc.",
    )
    parser.add_argument("--root", type=Path, default=Path("."), help="repo root to scan")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    findings = check(args.root)
    for finding in findings:
        print(str(finding))
    if findings:
        print(f"{len(findings)} toolchain-consistency finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
