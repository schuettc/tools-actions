#!/usr/bin/env python3
"""Build, verify and guard a project's container images *outside* `cdk deploy`.

CDK still owns the image identity: `cdk synth` writes `cdk.out/*.assets.json`,
and each `dockerImages.<id>` entry is content-addressed over its staged
context, Dockerfile, platform and build-secret *names*. This CLI reads those
manifests and drives `docker buildx` / `aws ecr` so the build runs once in CI
and prod promotes the artifact by digest — never a second build.

Everything project-specific — region, CDK bootstrap qualifier, dev/prod
accounts, the layer-cache repo, and the per-image cache key / build target /
deploy target — lives in a TOML config file (default ``ci/images/images.toml``,
override with ``--config``), never in this code. The code is verbatim across
consumers; only the config differs.

Subcommands (all read ``--cdk-out DIR`` and ``--config PATH``):

* ``list``           — one JSON line per image asset (its source + destination).
* ``build``          — ``docker buildx build`` each asset with the manifest's
                       directory, Dockerfile, platform, build args and secrets,
                       plus the registry layer-cache flags. ``--mode push`` tags
                       ``<registry>:<hash>`` and pushes; ``--mode load`` loads
                       locally only. ``--cache none|read|readwrite`` selects the
                       cache mode; the per-image cache ref is derived from config
                       as ``<cache_registry>/<cache_repo>:<cache_prefix>-<key>``.
* ``assert-present`` — the deploy guard: every asset's ``<hash>`` tag must exist
                       in the target account's repo, else exit 1 naming each
                       missing ``<stack>/<asset_id>:<hash>``.
* ``digests``        — one ``{"hash","digest"}`` line per asset (for promotion).
* ``promote``        — copy each asset's ``<hash>`` image from the dev bootstrap
                       repo to the prod one *by digest* (``docker buildx
                       imagetools create --prefer-index=false``), never a
                       rebuild. ``--from-account`` / ``--to-account`` default to
                       ``accounts.dev`` / ``accounts.prod`` in the config; when
                       they are equal (single-account projects) promotion only
                       verifies presence and copies nothing.
* ``check-deployed`` — post-deploy outcome gate: each prod container Lambda's or
                       Batch job definition's running image digest must equal the
                       dev digest for its asset hash, else exit 1 with a
                       per-resource report. Batch stores its image string verbatim
                       and never resolves a tag, so a ``:tag`` (no digest) image is
                       accepted only when its ECR repo is IMMUTABLE for that tag —
                       the CDK bootstrap container-assets repo is — and the tag
                       then resolves to a digest via ``ecr describe-images``.

The dev/prod bootstrap repo for an account is derived from the account id and
the config's ``bootstrap_qualifier`` / ``region``, not from the (stage-specific)
``repositoryName`` in a manifest: promotion synthesizes the *prod* manifests, yet
must read the *dev* repo for the same hash (dev and prod synthesize identical
hashes).

Media-type policy is per image, keyed on its config ``deploy_target``: a
``lambda`` image must be a single image manifest (Lambda rejects an OCI index);
a ``batch`` image may be any shape.

All subprocess calls funnel through `_run`, which raises `RunError` on a
non-zero exit — no silent fallbacks. The one exception is an absent ECR tag
(``ImageNotFoundException``), which `_describe_image` treats as a value ("not
present") rather than an error; any other describe failure still raises.

Python 3.12+ stdlib only (``tomllib`` is stdlib from 3.11).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# BuildKit registry cache accepts this format; the write flag needs the
# OCI/image-manifest options ECR requires.
_CACHE_TO_SUFFIX = "mode=max,image-manifest=true,oci-mediatypes=true"

# The single ECR result treated as a value rather than a failure.
_TAG_ABSENT_MARKER = "ImageNotFoundException"

_DEFAULT_CONFIG = Path("ci/images/images.toml")

# Config `image` entry `key` must be a lowercase dns-ish label.
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# An AWS account id is exactly 12 decimal digits.
_ACCOUNT_RE = re.compile(r"^[0-9]{12}$")

# `cache_prefix` becomes the head of a Docker tag `<cache_prefix>-<key>`; it must
# use valid Docker tag characters and start with an alphanumeric or underscore.
_TAG_PREFIX_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

_DEPLOY_TARGETS = frozenset({"lambda", "batch"})

# Manifest media types Lambda can deploy: a SINGLE image manifest. An OCI image
# index (what buildx pushes by default once it attaches provenance attestations)
# and attestation manifests are rejected by Lambda. Every Lambda-bound tag is
# checked against this set; a Batch image may be any shape.
_LAMBDA_MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
)


class ConfigError(Exception):
    """The images.toml config is missing or malformed. Names the offender."""


def _require(mapping: dict[str, object], key: str, *, where: str) -> object:
    """Return ``mapping[key]`` or raise `ConfigError` naming the missing key."""
    if key not in mapping:
        raise ConfigError(f"images config: missing required key {where!r}")
    return mapping[key]


def _dockerfiles_ambiguous(a: str, b: str) -> bool:
    """Whether two config Dockerfiles could both match one asset via `entry_for`.

    `entry_for` matches an asset Dockerfile ``d`` to a config entry ``e`` when
    ``d == e.dockerfile`` or ``d.endswith("/" + e.dockerfile)``. Two config
    entries collide when the same asset path could satisfy both: either the
    paths are equal, or one is a ``/``-boundary suffix of the other (e.g.
    ``Dockerfile`` vs ``docker/Dockerfile``).
    """
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _require_str(mapping: dict[str, object], key: str, *, where: str) -> str:
    """Return a required, non-empty string value or raise `ConfigError`.

    The copier validators in v0.2.0 required these keys to be non-empty strings;
    the loader is now the single enforcement point, so a value that is present
    but blank or of the wrong type (e.g. ``region = ""`` or a number) must fail
    loudly rather than silently producing a malformed registry/cache ref.
    """
    value = _require(mapping, key, where=where)
    if not isinstance(value, str):
        raise ConfigError(
            f"images config: {where!r} must be a non-empty string, got {type(value).__name__}"
        )
    if not value.strip():
        raise ConfigError(f"images config: {where!r} must be a non-empty string")
    return value


@dataclass(frozen=True)
class ImageEntry:
    """One ``[[image]]`` config row: a cache/build/deploy policy for a Dockerfile."""

    key: str
    dockerfile: str
    target: str
    deploy_target: str


@dataclass(frozen=True)
class Config:
    """The parsed, validated images.toml."""

    region: str
    bootstrap_qualifier: str
    cache_repo: str
    cache_prefix: str
    dev_account: str
    prod_account: str
    images: tuple[ImageEntry, ...]

    def entry_for(self, dockerfile: str, target: str) -> ImageEntry:
        """Return the ``[[image]]`` entry matching a ``(dockerfile, target)`` pair.

        A single Dockerfile can build several images by build target (CDK's
        ``dockerBuildTarget``): e.g. two Lambda images from one
        ``Dockerfile.lambda`` at targets ``light`` and ``conform``. Matching on
        the Dockerfile suffix alone would map both to whichever entry sorts
        first — one wrong cache ref and deploy policy for the other. So an entry
        matches only when its ``dockerfile`` suffix AND its ``target`` both
        equal the asset's.

        Zero matches → `ConfigError` naming the Dockerfile and target, so a new
        image can never silently share (or skip) a cache ref / media policy.
        More than one match → `ConfigError` "ambiguous" naming the candidate
        keys; there is no first-match-wins.
        """
        matches = [
            e
            for e in self.images
            if (dockerfile == e.dockerfile or dockerfile.endswith("/" + e.dockerfile))
            and e.target == target
        ]
        if not matches:
            raise ConfigError(
                f"images config: no [[image]] entry matches Dockerfile {dockerfile!r} "
                f"with build target {target!r}"
            )
        if len(matches) > 1:
            keys = [e.key for e in matches]
            raise ConfigError(
                f"images config: ambiguous [[image]] entries for Dockerfile {dockerfile!r} "
                f"with build target {target!r}: candidate keys {keys}"
            )
        return matches[0]

    def cache_ref(self, key: str) -> str | None:
        """The derived registry cache ref for one image, or None if no cache repo."""
        if not self.cache_repo:
            return None
        registry = f"{self.dev_account}.dkr.ecr.{self.region}.amazonaws.com"
        return f"{registry}/{self.cache_repo}:{self.cache_prefix}-{key}"


def _load_config(path: Path) -> Config:
    """Read and validate images.toml. Raises `ConfigError` on any problem."""
    if not path.exists():
        raise ConfigError(f"images config: file not found: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    region = _require_str(raw, "region", where="region")
    bootstrap_qualifier = _require_str(raw, "bootstrap_qualifier", where="bootstrap_qualifier")
    # cache_repo may be empty (no layer cache); cache_prefix is only required
    # when cache_repo is set (see below).
    cache_repo = _require(raw, "cache_repo", where="cache_repo")
    if not isinstance(cache_repo, str):
        raise ConfigError(
            f"images config: 'cache_repo' must be a string, got {type(cache_repo).__name__}"
        )
    cache_prefix = _require(raw, "cache_prefix", where="cache_prefix")
    if not isinstance(cache_prefix, str):
        raise ConfigError(
            f"images config: 'cache_prefix' must be a string, got {type(cache_prefix).__name__}"
        )
    # A non-empty cache_repo derives the tag `<cache_prefix>-<key>`; an empty or
    # invalid prefix would yield `:-<key>`, which is not a valid Docker tag.
    if cache_repo.strip():
        if not cache_prefix.strip():
            raise ConfigError(
                "images config: 'cache_prefix' must be a non-empty string when "
                "'cache_repo' is set (it heads the cache tag '<cache_prefix>-<key>')"
            )
        if not _TAG_PREFIX_RE.match(cache_prefix):
            raise ConfigError(
                f"images config: 'cache_prefix' {cache_prefix!r} must use valid Docker "
                f"tag characters (match {_TAG_PREFIX_RE.pattern})"
            )
    accounts = _require(raw, "accounts", where="accounts")
    if not isinstance(accounts, dict):
        raise ConfigError("images config: 'accounts' must be a table")
    dev_account = _require_str(accounts, "dev", where="accounts.dev")
    prod_account = _require_str(accounts, "prod", where="accounts.prod")
    for label, account in (("accounts.dev", dev_account), ("accounts.prod", prod_account)):
        if not _ACCOUNT_RE.match(account):
            raise ConfigError(
                f"images config: {label!r} {account!r} must be a 12-digit AWS account id"
            )

    raw_images = _require(raw, "image", where="image")
    if not isinstance(raw_images, list) or not raw_images:
        raise ConfigError("images config: 'image' must be a non-empty array of tables")

    seen_keys: set[str] = set()
    # Ambiguous [[image]] entries are rejected at load time: two entries that
    # could both match the same asset would make `entry_for` ambiguous. The
    # match uses the SAME rule as `entry_for` (suffix match on the Dockerfile
    # plus an equal target), so `Dockerfile` and `docker/Dockerfile` at the same
    # target — where an asset `.../docker/Dockerfile` matches both — are caught
    # here rather than only at run time. There is no copier validator any more,
    # so the loader is the single place that must reject a malformed
    # images.toml, naming the offending keys. (`entry_for` keeps its own
    # defensive ambiguity guard for directly-constructed Config objects.)
    entries: list[ImageEntry] = []
    for i, item in enumerate(raw_images):
        if not isinstance(item, dict):
            raise ConfigError(f"images config: image[{i}] must be a table")
        key = _require(item, "key", where=f"image[{i}].key")
        dockerfile = _require(item, "dockerfile", where=f"image[{i}].dockerfile")
        target = _require(item, "target", where=f"image[{i}].target")
        deploy_target = _require(item, "deploy_target", where=f"image[{i}].deploy_target")
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise ConfigError(f"images config: image[{i}].key {key!r} must match {_KEY_RE.pattern}")
        if key in seen_keys:
            raise ConfigError(f"images config: duplicate image key {key!r}")
        seen_keys.add(key)
        if deploy_target not in _DEPLOY_TARGETS:
            raise ConfigError(
                f"images config: image[{i}].deploy_target {deploy_target!r} "
                f"must be one of {sorted(_DEPLOY_TARGETS)}"
            )
        dockerfile = str(dockerfile)
        target = str(target)
        for prior in entries:
            if prior.target == target and _dockerfiles_ambiguous(prior.dockerfile, dockerfile):
                raise ConfigError(
                    f"images config: ambiguous [[image]] entries for Dockerfile "
                    f"{dockerfile!r} with build target {target!r}: keys "
                    f"{[prior.key, key]}"
                )
        entries.append(
            ImageEntry(
                key=key,
                dockerfile=dockerfile,
                target=target,
                deploy_target=str(deploy_target),
            )
        )

    return Config(
        region=str(region),
        bootstrap_qualifier=str(bootstrap_qualifier),
        cache_repo=str(cache_repo),
        cache_prefix=str(cache_prefix),
        dev_account=str(dev_account),
        prod_account=str(prod_account),
        images=tuple(entries),
    )


def _lambda_shape_error(label: str, media_type: str) -> str | None:
    """Return a loud error line if ``media_type`` is not Lambda-deployable."""
    if media_type in _LAMBDA_MANIFEST_MEDIA_TYPES:
        return None
    return (
        f"images: {label} has manifest media type {media_type}, which Lambda "
        f"cannot deploy (needs a single image manifest: "
        f"{', '.join(sorted(_LAMBDA_MANIFEST_MEDIA_TYPES))})"
    )


def _bootstrap_repo(account: str, qualifier: str, region: str) -> str:
    return f"cdk-{qualifier}-container-assets-{account}-{region}"


def _registry_uri(account: str, qualifier: str, region: str) -> str:
    return f"{account}.dkr.ecr.{region}.amazonaws.com/{_bootstrap_repo(account, qualifier, region)}"


class RunError(RuntimeError):
    """A subprocess exited non-zero. Carries the command and captured output."""

    def __init__(self, cmd: Sequence[str], returncode: int, stdout: str, stderr: str) -> None:
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"command failed ({returncode}): {' '.join(cmd)}\n{stderr.strip()}")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """The single subprocess call site. Raises `RunError` on non-zero exit.

    Inherits the process environment so ``docker buildx`` can read the
    ``CODEARTIFACT_AUTH_TOKEN`` secret and ``aws`` can read its credentials.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RunError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return proc


@dataclass(frozen=True)
class ImageAsset:
    """One `dockerImages.<id>` entry with its single destination, flattened."""

    stack: str
    asset_id: str
    hash: str
    directory: str
    dockerfile: str
    platform: str
    build_args: dict[str, str]
    build_secrets: dict[str, str]
    target: str
    repository: str
    region: str

    def to_json(self) -> dict[str, object]:
        return {
            "stack": self.stack,
            "asset_id": self.asset_id,
            "hash": self.hash,
            "directory": self.directory,
            "dockerfile": self.dockerfile,
            "platform": self.platform,
            "build_args": self.build_args,
            "build_secrets": self.build_secrets,
            "repository": self.repository,
        }


class ManifestError(Exception):
    """A manifest is malformed for build-once's purposes (bad destinations)."""


def _load_assets(cdk_out: Path, region: str) -> list[ImageAsset]:
    """Return every docker image asset across `DIR/*.assets.json`, sorted.

    Raises `ManifestError` if any asset does not have exactly one destination.
    Returns an empty list if there are no image assets at all (callers decide
    whether that is fatal for their subcommand).
    """
    assets: list[ImageAsset] = []
    for manifest_path in sorted(cdk_out.glob("*.assets.json")):
        stack = manifest_path.name.removesuffix(".assets.json")
        manifest = json.loads(manifest_path.read_text())
        for asset_id, entry in manifest.get("dockerImages", {}).items():
            source = entry["source"]
            destinations = entry.get("destinations", {})
            if len(destinations) != 1:
                raise ManifestError(
                    f"{stack}/{asset_id}: expected exactly one destination, "
                    f"found {len(destinations)}"
                )
            (destination,) = destinations.values()
            assets.append(
                ImageAsset(
                    stack=stack,
                    asset_id=asset_id,
                    hash=destination["imageTag"],
                    directory=source["directory"],
                    dockerfile=source["dockerFile"],
                    platform=source["platform"],
                    build_args=dict(source.get("dockerBuildArgs", {})),
                    build_secrets=dict(source.get("dockerBuildSecrets", {})),
                    target=source.get("dockerBuildTarget", "") or "",
                    repository=destination["repositoryName"],
                    region=destination.get("region", region),
                )
            )
    assets.sort(key=lambda a: (a.stack, a.asset_id))
    return assets


def _cache_flags(cache_mode: str, ref: str | None) -> list[str]:
    """buildx cache flags for one image.

    ``readwrite`` reads and writes the ref; ``read`` reads only (PRs never
    write shared cache); ``none`` uses no cache. A missing ref (empty
    ``cache_repo``) with a cache mode other than ``none`` fails loudly rather
    than building silently uncached.
    """
    if cache_mode == "none":
        return []
    if ref is None:
        raise ValueError(
            f"--cache {cache_mode} needs a cache ref, but cache_repo is empty in the "
            f"config (set cache_repo, or use --cache none)"
        )
    flags = ["--cache-from", f"type=registry,ref={ref}"]
    if cache_mode == "readwrite":
        flags += ["--cache-to", f"type=registry,ref={ref},{_CACHE_TO_SUFFIX}"]
    return flags


def _describe_image(
    repository: str, image_tag: str, *, registry_id: str | None, region: str
) -> dict[str, object] | None:
    """Return the ECR describe-images result, or None if the tag is absent.

    An absent tag surfaces as a non-zero exit with ``ImageNotFoundException``
    in stderr; that is the single non-zero result treated as a value. Anything
    else re-raises the `RunError`.
    """
    cmd = [
        "aws",
        "ecr",
        "describe-images",
        "--repository-name",
        repository,
        "--image-ids",
        f"imageTag={image_tag}",
        "--region",
        region,
    ]
    if registry_id is not None:
        cmd += ["--registry-id", registry_id]
    try:
        proc = _run(cmd)
    except RunError as err:
        if _TAG_ABSENT_MARKER in err.stderr:
            return None
        raise
    result: dict[str, object] = json.loads(proc.stdout)
    return result


def _digest_of(describe_result: dict[str, object]) -> str:
    """Pull the single image digest out of an ECR describe-images result."""
    details = describe_result["imageDetails"]
    assert isinstance(details, list)
    digest = details[0]["imageDigest"]
    assert isinstance(digest, str)
    return digest


def _media_type_of(describe_result: dict[str, object]) -> str:
    """Pull the manifest media type out of an ECR describe-images result."""
    details = describe_result["imageDetails"]
    assert isinstance(details, list)
    media_type = details[0]["imageManifestMediaType"]
    assert isinstance(media_type, str)
    return media_type


def _describe_repository(
    repository: str, *, registry_id: str, region: str
) -> dict[str, object]:
    """Return the ECR ``describe-repositories`` record for one repository.

    Any failure (repo absent, auth, network) re-raises the `RunError`; a missing
    bootstrap repo is a real error here, not a value.
    """
    proc = _run(
        [
            "aws",
            "ecr",
            "describe-repositories",
            "--repository-names",
            repository,
            "--registry-id",
            registry_id,
            "--region",
            region,
        ]
    )
    repositories = json.loads(proc.stdout).get("repositories", [])
    assert isinstance(repositories, list) and repositories
    record = repositories[0]
    assert isinstance(record, dict)
    return record


def _exclusion_matches_tag(exclusion: object, tag: str) -> bool:
    """Whether one ECR tag-mutability exclusion filter matches ``tag``.

    An ``IMMUTABLE_WITH_EXCLUSION`` repository carries
    ``imageTagMutabilityExclusionFilters``: each is
    ``{imageTagMutabilityExclusionFilterType, imageTagMutabilityExclusionFilterValue}``
    where the only documented type is ``WILDCARD`` (``*`` matches any run). A tag
    the filter matches is *mutable* even in an otherwise-immutable repo. Any
    filter shape we do not understand is treated conservatively as matching, so
    an unrecognized exclusion makes us refuse the tag rather than trust it.
    """
    if not isinstance(exclusion, dict):
        return True
    filter_type = exclusion.get("imageTagMutabilityExclusionFilterType")
    value = exclusion.get("imageTagMutabilityExclusionFilterValue")
    if filter_type != "WILDCARD" or not isinstance(value, str):
        return True
    pattern = "^" + ".*".join(re.escape(part) for part in value.split("*")) + "$"
    return re.match(pattern, tag) is not None


def _repo_tag_is_immutable(repository_record: dict[str, object], tag: str) -> bool:
    """Whether ``tag`` is an immutable identity in this ECR repository.

    ``IMMUTABLE`` guarantees every tag is write-once. ``IMMUTABLE_WITH_EXCLUSION``
    guarantees it only for tags no exclusion filter matches. Everything else
    (``MUTABLE``, ``MUTABLE_WITH_EXCLUSION``, or an unknown value) means the tag
    could be repointed, so it is not a stable identity.
    """
    mutability = repository_record.get("imageTagMutability")
    if mutability == "IMMUTABLE":
        return True
    if mutability == "IMMUTABLE_WITH_EXCLUSION":
        filters = repository_record.get("imageTagMutabilityExclusionFilters", [])
        if not isinstance(filters, list):
            return False
        return not any(_exclusion_matches_tag(f, tag) for f in filters)
    return False


# `docker buildx imagetools inspect` emits exactly this suffix on stderr when a
# tag is absent from the registry (``ERROR: <ref>: not found``). That signature
# - matched against the *exact ref* being inspected (``f"{ref}: not found"``) -
# is treated as "absent"; any other inspect failure (a *repository* not found,
# auth, network, malformed ref) does NOT match and re-raises.
_REGISTRY_TAG_ABSENT_SUFFIX = ": not found"


def _registry_digest(ref: str) -> dict[str, str] | None:
    """Return the ``{digest, mediaType}`` of a registry ref, or None if absent.

    Reads purely at the registry level via ``docker buildx imagetools inspect
    <ref> --format '{{json .Manifest}}'`` - through docker's per-registry login,
    never the AWS ECR API - so the same call reads the dev and the prod registry
    regardless of which AWS identity is active.

    An absent tag surfaces as a non-zero exit whose stderr ends with exactly
    ``<ref>: not found`` (the ref being inspected) - the single non-zero result
    treated as a value; any other failure re-raises the `RunError`.
    """
    try:
        proc = _run(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                ref,
                "--format",
                "{{json .Manifest}}",
            ]
        )
    except RunError as err:
        if f"{ref}{_REGISTRY_TAG_ABSENT_SUFFIX}" in err.stderr:
            return None
        raise
    manifest = json.loads(proc.stdout)
    digest = manifest["digest"]
    media_type = manifest["mediaType"]
    assert isinstance(digest, str)
    assert isinstance(media_type, str)
    return {"digest": digest, "mediaType": media_type}


def _build_argv(
    asset: ImageAsset,
    cdk_out: Path,
    *,
    registry: str | None,
    mode: str,
    cache_flags: list[str],
    target: str,
) -> list[str]:
    context = cdk_out / asset.directory
    dockerfile = context / asset.dockerfile
    argv = [
        "docker",
        "buildx",
        "build",
        "--file",
        str(dockerfile),
        "--platform",
        asset.platform,
    ]
    if target:
        argv += ["--target", target]
    for key, value in sorted(asset.build_args.items()):
        argv += ["--build-arg", f"{key}={value}"]
    for key, spec in sorted(asset.build_secrets.items()):
        argv += ["--secret", f"id={key},{spec}"]
    # No provenance/SBOM attestations: buildx attaches provenance by default on
    # the docker-container driver, which wraps the image in an OCI index Lambda
    # rejects. Off in both modes so PR verification builds the same shape that
    # ships.
    argv += ["--provenance=false", "--sbom=false"]
    argv += cache_flags
    if mode == "push":
        if not registry or registry == "none":
            raise ValueError("--mode push requires --registry")
        # Explicit image exporter with docker media types: one docker-v2
        # manifest, the exact shape every Lambda deploy needs.
        argv += [
            "--output",
            f"type=image,name={registry}:{asset.hash},push=true,oci-mediatypes=false",
        ]
    else:  # load
        argv.append("--load")
    argv.append(str(context))
    return argv


def _cmd_list(args: argparse.Namespace) -> int:
    # `list` reports asset shape only; it needs no project config. The region is
    # only a display default for the asset's stored region.
    assets = _load_assets(args.cdk_out, "")
    if not assets:
        print("images: no docker image assets found", file=sys.stderr)
        return 1
    for asset in assets:
        print(json.dumps(asset.to_json()))
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    assets = _load_assets(args.cdk_out, config.region)
    if not assets:
        print("images: no docker image assets found", file=sys.stderr)
        return 1

    skip_existing = args.skip_existing and args.mode == "push"

    for asset in assets:
        try:
            entry = config.entry_for(asset.dockerfile, asset.target)
            cache_flags = _cache_flags(args.cache, config.cache_ref(entry.key))
        except (ConfigError, ValueError) as err:
            print(f"images: {err}", file=sys.stderr)
            return 1

        label = f"{asset.stack}/{asset.asset_id}:{asset.hash}"
        ref = f"{args.registry}:{asset.hash}"
        is_lambda = entry.deploy_target == "lambda"
        if skip_existing:
            existing = _describe_image(
                asset.repository, asset.hash, registry_id=None, region=config.region
            )
            if existing is not None:
                # A present tag is only skippable if its shape is deployable. The
                # bootstrap repo is IMMUTABLE, so a wrong-shape Lambda tag can
                # neither be skipped nor overwritten: fail loudly and name the
                # one remedy. Batch accepts any shape.
                shape_error = (
                    _lambda_shape_error(label, _media_type_of(existing)) if is_lambda else None
                )
                if shape_error is not None:
                    print(
                        f"{shape_error}; the repository's tags are immutable, so delete "
                        f"it (aws ecr batch-delete-image --repository-name "
                        f"{asset.repository} --image-ids imageTag={asset.hash}) and re-run",
                        file=sys.stderr,
                    )
                    return 1
                print(f"images: {label} action=skipped-existing", file=sys.stderr)
                continue

        argv = _build_argv(
            asset,
            args.cdk_out,
            registry=args.registry,
            mode=args.mode,
            cache_flags=cache_flags,
            target=entry.target,
        )
        print(
            f"images: {label} action=built cache={args.cache} flags={cache_flags}",
            file=sys.stderr,
        )
        _run(argv)

        if args.mode == "push" and is_lambda:
            pushed = _registry_digest(ref)
            if pushed is None:
                print(f"images: {label} absent from {args.registry} after push", file=sys.stderr)
                return 1
            shape_error = _lambda_shape_error(label, pushed["mediaType"])
            if shape_error is not None:
                print(f"{shape_error} (as pushed)", file=sys.stderr)
                return 1
    return 0


def _cmd_assert_present(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    assets = _load_assets(args.cdk_out, config.region)
    if not assets:
        print("images: no docker image assets found", file=sys.stderr)
        return 1
    missing: list[str] = []
    wrong_shape: list[str] = []
    for asset in assets:
        label = f"{asset.stack}/{asset.asset_id}:{asset.hash}"
        entry = config.entry_for(asset.dockerfile, asset.target)
        result = _describe_image(
            asset.repository, asset.hash, registry_id=args.account, region=config.region
        )
        if result is None:
            missing.append(label)
            continue
        if entry.deploy_target == "lambda":
            shape_error = _lambda_shape_error(label, _media_type_of(result))
            if shape_error is not None:
                wrong_shape.append(shape_error)
    if missing:
        print(
            f"images: missing image(s) in account {args.account}: {', '.join(missing)}",
            file=sys.stderr,
        )
    for line in wrong_shape:
        print(line, file=sys.stderr)
    return 1 if missing or wrong_shape else 0


def _cmd_digests(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    assets = _load_assets(args.cdk_out, config.region)
    if not assets:
        print("images: no docker image assets found", file=sys.stderr)
        return 1
    for asset in assets:
        result = _describe_image(
            asset.repository, asset.hash, registry_id=args.account, region=config.region
        )
        if result is None:
            print(
                f"images: {asset.stack}/{asset.asset_id}:{asset.hash} not present "
                f"in account {args.account}",
                file=sys.stderr,
            )
            return 1
        digest = _digest_of(result)
        print(json.dumps({"hash": asset.hash, "digest": digest}))
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    """Copy each asset's <hash> image dev ECR -> prod ECR, by digest.

    ``--from-account`` / ``--to-account`` default to ``accounts.dev`` /
    ``accounts.prod`` in the config; an explicit flag overrides. When the two are
    equal (a single-account project) promotion only verifies each source is
    present and copies nothing.

    Every read is a registry-level ``imagetools inspect`` (via `_registry_digest`),
    never the AWS ECR API, so this works cross-account under a single AWS
    identity.
    """
    config = _load_config(args.config)
    from_account = args.from_account or config.dev_account
    to_account = args.to_account or config.prod_account
    assets = _load_assets(args.cdk_out, config.region)
    if not assets:
        print("images: no docker image assets found", file=sys.stderr)
        return 1
    dev_registry = _registry_uri(from_account, config.bootstrap_qualifier, config.region)
    prod_registry = _registry_uri(to_account, config.bootstrap_qualifier, config.region)
    # Pass 1: read + validate EVERY dev source before copying anything, so a
    # bad source never leaves prod half-promoted.
    sources: dict[str, dict[str, str]] = {}
    for asset in assets:
        label = f"{asset.stack}/{asset.asset_id}:{asset.hash}"
        entry = config.entry_for(asset.dockerfile, asset.target)
        dev = _registry_digest(f"{dev_registry}:{asset.hash}")
        if dev is None:
            print(
                f"images: {label} not present in dev account {from_account}; "
                "promotion never builds",
                file=sys.stderr,
            )
            return 1
        # Never promote a shape a Lambda cannot deploy. A Batch image may be any
        # shape.
        if entry.deploy_target == "lambda":
            shape_error = _lambda_shape_error(f"{label} (dev source)", dev["mediaType"])
            if shape_error is not None:
                print(shape_error, file=sys.stderr)
                return 1
        sources[asset.hash] = dev

    # Pass 2: copy.
    dev_digests: dict[str, str] = {}
    for asset in assets:
        label = f"{asset.stack}/{asset.asset_id}:{asset.hash}"
        prod_ref = f"{prod_registry}:{asset.hash}"
        dev_digest = sources[asset.hash]["digest"]
        dev_media_type = sources[asset.hash]["mediaType"]
        dev_digests[asset.hash] = dev_digest

        prod = _registry_digest(prod_ref)
        if prod is not None:
            if prod["digest"] == dev_digest:
                print(f"images: {label} action=skip-present digest={dev_digest}", file=sys.stderr)
                continue
            print(
                f"images: {label} prod digest {prod['digest']} != dev digest {dev_digest}; "
                "an image for these inputs was built elsewhere",
                file=sys.stderr,
            )
            return 1

        argv = [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--prefer-index=false",
            "--tag",
            prod_ref,
            f"{dev_registry}@{dev_digest}",
        ]
        print(
            f"images: {label} action=copy from={dev_registry}@{dev_digest} to={prod_registry}",
            file=sys.stderr,
        )
        _run(argv)

        copied = _registry_digest(prod_ref)
        if copied is None:
            print(f"images: {label} absent in prod after copy", file=sys.stderr)
            return 1
        if copied["digest"] != dev_digest:
            print(
                f"images: {label} copied digest {copied['digest']} != dev digest {dev_digest}",
                file=sys.stderr,
            )
            return 1
        if copied["mediaType"] != dev_media_type:
            print(
                f"images: {label} copied media type {copied['mediaType']} != "
                f"dev media type {dev_media_type} (single manifest must stay single)",
                file=sys.stderr,
            )
            return 1
        print(
            f"images: {label} action=copied digest={copied['digest']} "
            f"mediaType={copied['mediaType']}",
            file=sys.stderr,
        )

    # Only reached once every asset copied/verified: emit the verified map.
    payload = json.dumps(dev_digests, separators=(",", ":"))
    print(payload)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as fh:
            fh.write(f"dev-digests={payload}\n")
    return 0


def _hash_from_image_uri(image_uri: object) -> str:
    """Extract the <hash> tag from a container image URI in a synthesized template.

    CDK renders it in several shapes:
    * a plain string ``<registry>/<repo>:<hash>``;
    * ``{"Fn::Sub": "<registry>/<repo>:<hash>"}`` (or ``[template, vars]``) - the
      Lambda ``Code.ImageUri`` form;
    * ``{"Fn::Join": ["", [parts...]]}`` - the Batch ``ContainerProperties.Image``
      form, where the ECR URI and ``:<hash>`` are assembled from intrinsics and
      string literals (aws-cdk-lib EcsJobDefinition, verified against a live
      ``AWS::Batch::JobDefinition`` template).

    The ``:<hash>`` tag is always a literal string, so joining the string parts
    and taking the segment after the last ``:`` recovers it. Anything with no
    ``:`` tag is a malformed/unresolvable image for our purposes.
    """
    if isinstance(image_uri, str):
        text = image_uri
    elif isinstance(image_uri, dict) and "Fn::Sub" in image_uri:
        sub = image_uri["Fn::Sub"]
        text = sub[0] if isinstance(sub, list) and sub else sub
    elif isinstance(image_uri, dict) and "Fn::Join" in image_uri:
        join = image_uri["Fn::Join"]
        if not (isinstance(join, list) and len(join) == 2 and isinstance(join[1], list)):
            raise ManifestError(f"unrecognized Fn::Join image: {image_uri!r}")
        separator, parts = join
        if not isinstance(separator, str):
            raise ManifestError(f"unrecognized Fn::Join image: {image_uri!r}")
        text = separator.join(p for p in parts if isinstance(p, str))
    else:
        raise ManifestError(f"unrecognized container image: {image_uri!r}")
    if not isinstance(text, str) or ":" not in text:
        raise ManifestError(f"container image has no <hash> tag: {image_uri!r}")
    return text.rsplit(":", 1)[1]


@dataclass(frozen=True)
class DeployedContainer:
    """One container resource discovered in a synthesized template."""

    stack: str
    logical_id: str
    kind: str  # "lambda" | "batch"
    hash: str


def _batch_image_of(properties: dict[str, object]) -> object | None:
    """Return the container image of an ``AWS::Batch::JobDefinition`` properties.

    ``ContainerProperties.Image`` is the single-container EcsJobDefinition shape
    (aws-cdk-lib EcsJobDefinition -> CfnJobDefinition.containerProperties.image);
    ``EcsProperties.TaskProperties[].Containers[].Image`` is the multi-container
    ``EcsProperties`` shape. None if neither carries an image.
    """
    container_properties = properties.get("ContainerProperties")
    if isinstance(container_properties, dict) and "Image" in container_properties:
        return container_properties["Image"]
    ecs_properties = properties.get("EcsProperties")
    if isinstance(ecs_properties, dict):
        for task in ecs_properties.get("TaskProperties", []):
            if not isinstance(task, dict):
                continue
            for container in task.get("Containers", []):
                if isinstance(container, dict) and "Image" in container:
                    return container["Image"]
    return None


def _load_deployed_containers(cdk_out: Path) -> list[DeployedContainer]:
    """Every container Lambda and Batch job definition across the synthesized
    ``DIR/*.template.json`` files, sorted for stable output.

    The stack name is the template filename; the logical id and hash are read
    straight from the template so the CDK-appended suffix is never hardcoded.
    """
    containers: list[DeployedContainer] = []
    for template_path in sorted(cdk_out.glob("*.template.json")):
        stack = template_path.name.removesuffix(".template.json")
        template = json.loads(template_path.read_text())
        for logical_id, resource in template.get("Resources", {}).items():
            resource_type = resource.get("Type")
            properties = resource.get("Properties", {})
            if resource_type == "AWS::Lambda::Function":
                image_uri = properties.get("Code", {}).get("ImageUri")
                if image_uri is None:
                    continue
                containers.append(
                    DeployedContainer(
                        stack=stack,
                        logical_id=logical_id,
                        kind="lambda",
                        hash=_hash_from_image_uri(image_uri),
                    )
                )
            elif resource_type == "AWS::Batch::JobDefinition":
                image = _batch_image_of(properties)
                if image is None:
                    continue
                containers.append(
                    DeployedContainer(
                        stack=stack,
                        logical_id=logical_id,
                        kind="batch",
                        hash=_hash_from_image_uri(image),
                    )
                )
    containers.sort(key=lambda c: (c.stack, c.logical_id))
    return containers


def _describe_stack_resource(stack: str, logical_id: str, region: str) -> str | None:
    """Resolve a logical id to its deployed physical name, or None if absent."""
    proc = _run(
        [
            "aws",
            "cloudformation",
            "describe-stack-resource",
            "--stack-name",
            stack,
            "--logical-resource-id",
            logical_id,
            "--region",
            region,
        ]
    )
    detail = json.loads(proc.stdout).get("StackResourceDetail", {})
    physical = detail.get("PhysicalResourceId")
    return physical if physical else None


def _resolved_lambda_digest(function_name: str, region: str) -> str | None:
    """``lambda get-function`` -> the digest of ``Code.ResolvedImageUri``.

    Returns None if the function has no resolved image digest so the caller can
    name the item.
    """
    proc = _run(
        [
            "aws",
            "lambda",
            "get-function",
            "--function-name",
            function_name,
            "--region",
            region,
        ]
    )
    uri = json.loads(proc.stdout).get("Code", {}).get("ResolvedImageUri")
    if not isinstance(uri, str) or "@" not in uri:
        return None
    return uri.split("@", 1)[1]


def _deployed_batch_image(definition: dict[str, object]) -> str | None:
    """Return the container image string a deployed Batch job definition runs.

    ``batch describe-job-definitions`` echoes the image verbatim under the
    single-container ``containerProperties.image`` shape or, for the
    multi-container ``ecsProperties`` shape, under
    ``ecsProperties.taskProperties[].containers[].image`` (lowercase, unlike the
    CloudFormation template's ``ContainerProperties`` / ``EcsProperties``). None
    if neither carries an image.
    """
    container_properties = definition.get("containerProperties")
    if isinstance(container_properties, dict):
        image = container_properties.get("image")
        if isinstance(image, str):
            return image
    ecs_properties = definition.get("ecsProperties")
    if isinstance(ecs_properties, dict):
        task_properties = ecs_properties.get("taskProperties", [])
        if isinstance(task_properties, list):
            for task in task_properties:
                if not isinstance(task, dict):
                    continue
                for container in task.get("containers", []):
                    if isinstance(container, dict):
                        image = container.get("image")
                        if isinstance(image, str):
                            return image
    return None


def _resolved_batch_digest(
    job_definition: str, config: Config
) -> tuple[str | None, str | None]:
    """Resolve a deployed Batch job definition's running image to a digest.

    Returns ``(digest, None)`` on success or ``(None, reason)`` naming why the
    image cannot be resolved to a trustworthy digest. AWS Batch stores the image
    string verbatim and never resolves a tag (unlike Lambda's
    ``ResolvedImageUri``), so:

    * ``<repo>@sha256:...`` (with or without a leading ``:tag``) resolves to that
      digest directly.
    * ``<repo>:tag`` (no digest) is trustworthy only when the repository is
      IMMUTABLE for that tag — CDK's ``ContainerImage.fromDockerImageAsset``
      renders ``<bootstrap-repo>:<asset-hash>`` and the CDK bootstrap
      container-assets repo is created IMMUTABLE, making the tag a write-once
      identity. We verify the repo mutability, then resolve the tag to its
      digest via ``ecr describe-images``.
    * anything with neither a tag nor a digest, or in a registry/account other
      than the expected one, is rejected.
    """
    proc = _run(
        [
            "aws",
            "batch",
            "describe-job-definitions",
            "--job-definitions",
            job_definition,
            "--region",
            config.region,
        ]
    )
    definitions = json.loads(proc.stdout).get("jobDefinitions", [])
    if not definitions:
        return None, "not found in deployed Batch"
    image = _deployed_batch_image(definitions[0])
    if not isinstance(image, str) or not image:
        return None, "no resolved image digest"
    if "@" in image:
        return image.split("@", 1)[1], None

    # A tag with no digest. Parse the ECR registry URI: only an immutable-repo
    # tag in the expected account/region is a stable identity we can resolve.
    host, sep, path = image.partition("/")
    if not sep or ":" not in path:
        return None, f"image {image!r} has neither a tag nor a digest"
    repository, tag = path.rsplit(":", 1)
    host_parts = host.split(".")
    if len(host_parts) < 5 or host_parts[1] != "dkr" or host_parts[2] != "ecr":
        return None, f"image {image!r} is not an ECR registry"
    account, host_region = host_parts[0], host_parts[3]
    expected_repo = _bootstrap_repo(config.prod_account, config.bootstrap_qualifier, config.region)
    if account != config.prod_account or host_region != config.region:
        return None, (
            f"image {image!r} is a different registry than expected "
            f"({config.prod_account}.dkr.ecr.{config.region}.amazonaws.com/{expected_repo})"
        )
    repository_record = _describe_repository(repository, registry_id=account, region=host_region)
    if not _repo_tag_is_immutable(repository_record, tag):
        mutability = repository_record.get("imageTagMutability")
        return None, (
            f"image {image!r} is a tag in a mutable repository "
            f"(imageTagMutability={mutability!r}); a tag is not a stable identity"
        )
    described = _describe_image(repository, tag, registry_id=account, region=host_region)
    if described is None:
        return None, f"image {image!r} tag {tag!r} not found in repository {repository!r}"
    return _digest_of(described), None


def _cmd_check_deployed(args: argparse.Namespace) -> int:
    """Assert each prod container Lambda / Batch job definition runs the dev digest.

    ``--function-map`` is a JSON object mapping each image asset ``<hash>`` to
    the digest dev built for it. For each container resource: resolve its
    physical name from the deployed stack, read the running image's digest, and
    compare it to the mapped dev digest. Every missing/mismatch/unresolvable path
    is collected and reported; a non-empty report exits 1.
    """
    config = _load_config(args.config)
    expected: dict[str, str] = json.loads(args.function_map)
    containers = _load_deployed_containers(args.cdk_out)
    if not containers:
        print("images: no container Lambda or Batch resources found", file=sys.stderr)
        return 1

    problems: list[str] = []
    for container in containers:
        name = f"{container.stack}/{container.logical_id}"
        want = expected.get(container.hash)
        if want is None:
            problems.append(f"{name}: no dev digest for hash {container.hash}")
            continue
        physical = _describe_stack_resource(container.stack, container.logical_id, config.region)
        if physical is None:
            problems.append(f"{name}: not found in deployed stack")
            continue
        if container.kind == "lambda":
            running = _resolved_lambda_digest(physical, config.region)
        else:
            running, reason = _resolved_batch_digest(physical, config)
            if reason is not None:
                problems.append(f"{name} ({physical}): {reason}")
                continue
        if running is None:
            problems.append(f"{name} ({physical}): no resolved image digest")
            continue
        if running != want:
            problems.append(f"{name} ({physical}): runs {running}, expected dev digest {want}")
            continue
        print(f"images: {name} ({physical}) runs dev digest {running}", file=sys.stderr)

    if problems:
        for problem in problems:
            print(f"images: {problem}", file=sys.stderr)
        print(
            f"images: {len(problems)} container resource(s) do not run the dev digest",
            file=sys.stderr,
        )
        return 1
    return 0


class _Parser(argparse.ArgumentParser):
    """argparse parser that also enforces cross-argument rules at parse time.

    ``build --mode push`` requires ``--registry`` (a real registry, not the
    ``none`` load-mode sentinel). Enforcing it here — not only at run time in
    ``_build_argv`` — means the README-snippet test (which merely *parses* each
    snippet's argv) rejects a push snippet that forgot ``--registry``.
    """

    def parse_known_args(  # type: ignore[override]
        self,
        args: Sequence[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> tuple[argparse.Namespace, list[str]]:
        ns, extras = super().parse_known_args(args, namespace)
        if (
            getattr(ns, "command", None) == "build"
            and getattr(ns, "mode", None) == "push"
            and (not getattr(ns, "registry", None) or ns.registry == "none")
        ):
            self.error("build --mode push requires --registry")
        return ns, extras


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--cdk-out",
            required=True,
            type=Path,
            help="cdk.out directory holding the synthesized *.assets.json files",
        )
        p.add_argument(
            "--config",
            type=Path,
            default=_DEFAULT_CONFIG,
            help="path to images.toml (default ci/images/images.toml)",
        )

    p_list = sub.add_parser("list", help="print one JSON line per image asset")
    _add_common(p_list)
    p_list.set_defaults(func=_cmd_list)

    p_build = sub.add_parser("build", help="buildx build each image asset")
    _add_common(p_build)
    p_build.add_argument(
        "--registry",
        default=None,
        help="registry to tag/push in push mode (ignored in load mode)",
    )
    p_build.add_argument("--mode", required=True, choices=["push", "load"])
    p_build.add_argument("--cache", required=True, choices=["readwrite", "read", "none"])
    p_build.add_argument(
        "--skip-existing",
        action="store_true",
        help="push mode: skip an image whose <hash> tag already exists in ECR",
    )
    p_build.set_defaults(func=_cmd_build)

    p_assert = sub.add_parser(
        "assert-present", help="deploy guard: every <hash> tag must exist in ECR"
    )
    _add_common(p_assert)
    p_assert.add_argument("--account", required=True, help="target ECR account id")
    p_assert.set_defaults(func=_cmd_assert_present)

    p_digests = sub.add_parser("digests", help="print {hash,digest} per image asset")
    _add_common(p_digests)
    p_digests.add_argument("--account", required=True, help="target ECR account id")
    p_digests.set_defaults(func=_cmd_digests)

    p_promote = sub.add_parser(
        "promote", help="copy each <hash> image dev ECR -> prod ECR by digest"
    )
    _add_common(p_promote)
    p_promote.add_argument(
        "--from-account",
        default=None,
        help="dev (source) account id (default: accounts.dev from config)",
    )
    p_promote.add_argument(
        "--to-account",
        default=None,
        help="prod (destination) account id (default: accounts.prod from config)",
    )
    p_promote.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help=(
            "append 'dev-digests=<json>' to this file after all copies succeed "
            "(the verified {hash: dev_digest} map for the post-deploy check)"
        ),
    )
    p_promote.set_defaults(func=_cmd_promote)

    p_check = sub.add_parser(
        "check-deployed",
        help="assert each prod container Lambda/Batch resource runs the dev digest",
    )
    _add_common(p_check)
    p_check.add_argument(
        "--function-map",
        required=True,
        help="JSON object mapping each image asset <hash> to the dev digest it built",
    )
    p_check.set_defaults(func=_cmd_check_deployed)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
    except (ManifestError, ConfigError) as err:
        print(f"images: {err}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
