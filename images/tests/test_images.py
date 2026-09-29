"""Tests for the rendered delivery-kit ``images.py`` (Task 2).

``images.py`` reads CDK's synthesized asset manifests (``cdk.out/*.assets.json``)
and builds / verifies / guards the container images outside ``cdk deploy``. It is
config-driven (``images.toml``) and Batch-aware.

These are the platform's 37 ``test_images.py`` tests ported with generic values
and the ``--config`` form, plus the new config/Batch tests. The module imports the
*verbatim* rendered ``images.py`` straight from the kit template directory, so it
runs standalone under any Python 3.12+ (``uvx --python 3.12 --with pytest pytest
tests/kits/delivery/test_images.py``) with only pytest — no muda, no copier.

``_run`` is the single subprocess call site; every test monkeypatches it to
record argv rather than shelling out to docker/aws.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# images.py lives beside this action's directory; import it straight from there
# so the suite runs standalone under any Python 3.12+ with only pytest.
_IMAGES_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_IMAGES_DIR))

import images  # noqa: E402

# --------------------------------------------------------------------------- #
# Generic project values (no project facts; standard §3a.2).
# --------------------------------------------------------------------------- #
REGION = "us-east-1"
QUALIFIER = "hnb659fds"
DEV_ACCOUNT = "111111111111"
PROD_ACCOUNT = "222222222222"
CACHE_REPO = "example-buildcache"
CACHE_PREFIX = "example-repo"

A_HASH = "aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff6666aaaa7777bbbb8888"
B_HASH = "1111aaaa2222bbbb3333cccc4444dddd5555eeee6666ffff7777aaaa8888bbbb"
HOST = "example-111111111111.d.codeartifact.us-east-1.amazonaws.com"
REPO = f"cdk-{QUALIFIER}-container-assets-{DEV_ACCOUNT}-{REGION}"
REGISTRY = f"{DEV_ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{REPO}"

A_DOCKERFILE = "services/svc-a/Dockerfile"
B_DOCKERFILE = "services/svc-b/Dockerfile"

CACHE_REGISTRY = f"{DEV_ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
A_REF = f"{CACHE_REGISTRY}/{CACHE_REPO}:{CACHE_PREFIX}-svc-a"
B_REF = f"{CACHE_REGISTRY}/{CACHE_REPO}:{CACHE_PREFIX}-svc-b"

DOCKER_V2 = "application/vnd.docker.distribution.manifest.v2+json"
OCI_IMAGE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_IMAGE_INDEX = "application/vnd.oci.image.index.v1+json"

_DEFAULT_IMAGES = [
    {"key": "svc-a", "dockerfile": A_DOCKERFILE, "target": "", "deploy_target": "lambda"},
    {"key": "svc-b", "dockerfile": B_DOCKERFILE, "target": "", "deploy_target": "lambda"},
]


def _config_text(
    images_rows: list[dict[str, str]] = _DEFAULT_IMAGES,
    *,
    region: str = REGION,
    qualifier: str = QUALIFIER,
    cache_repo: str = CACHE_REPO,
    cache_prefix: str = CACHE_PREFIX,
    dev: str = DEV_ACCOUNT,
    prod: str = PROD_ACCOUNT,
    drop: str | None = None,
) -> str:
    lines: list[str] = []
    for key, value in (
        ("region", region),
        ("bootstrap_qualifier", qualifier),
        ("cache_repo", cache_repo),
        ("cache_prefix", cache_prefix),
    ):
        if drop != key:
            lines.append(f'{key} = "{value}"')
    if drop != "accounts":
        lines.append("[accounts]")
        if drop != "accounts.dev":
            lines.append(f'dev = "{dev}"')
        if drop != "accounts.prod":
            lines.append(f'prod = "{prod}"')
    for row in images_rows:
        lines.append("[[image]]")
        for key in ("key", "dockerfile", "target", "deploy_target"):
            lines.append(f'{key} = "{row[key]}"')
    return "\n".join(lines) + "\n"


def _write_config(tmp_path: Path, **kwargs: Any) -> Path:
    cfg = tmp_path / "images.toml"
    cfg.write_text(_config_text(**kwargs))
    return cfg


def _image_manifest(
    asset_id: str,
    dockerfile: str,
    *,
    target: str = "",
    destinations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if destinations is None:
        destinations = {
            f"{DEV_ACCOUNT}-{REGION}-c91f81b4": {
                "repositoryName": REPO,
                "imageTag": asset_id,
                "region": REGION,
                "assumeRoleArn": f"arn:${{AWS::Partition}}:iam::{DEV_ACCOUNT}:role/publish",
            }
        }
    source: dict[str, Any] = {
        "directory": f"asset.{asset_id}",
        "dockerBuildArgs": {"CODEARTIFACT_INDEX_HOST": HOST},
        "dockerBuildSecrets": {"ca_token": "env=CODEARTIFACT_AUTH_TOKEN"},
        "dockerFile": dockerfile,
        "platform": "linux/arm64",
    }
    # CDK writes dockerBuildTarget only when the asset sets a build target.
    if target:
        source["dockerBuildTarget"] = target
    return {
        "version": "54.0.0",
        "files": {
            "tmpl": {
                "displayName": "Template",
                "source": {"path": "Stack.template.json", "packaging": "file"},
                "destinations": {"d": {"bucketName": "b", "objectKey": "k"}},
            }
        },
        "dockerImages": {
            asset_id: {
                "displayName": "Function/AssetImage",
                "source": source,
                "destinations": destinations,
            }
        },
    }


def _write_cdk_out(
    tmp_path: Path,
    manifests: dict[str, dict[str, Any]] | None = None,
) -> Path:
    cdk_out = tmp_path / "cdk.out"
    cdk_out.mkdir(parents=True)
    if manifests is None:
        manifests = {
            "SvcA-dev": _image_manifest(A_HASH, A_DOCKERFILE),
            "SvcB-dev": _image_manifest(B_HASH, B_DOCKERFILE),
        }
    for stack, manifest in manifests.items():
        (cdk_out / f"{stack}.assets.json").write_text(json.dumps(manifest))
    return cdk_out


class _Recorder:
    """A fake ``_run`` that records argv and answers registry/ECR shape queries.

    ``existing`` tags answer ECR ``describe-images`` and registry-level
    ``imagetools inspect``; ``media`` overrides a tag's manifest media type
    (default a single docker v2 manifest). A ``buildx build`` push records its
    tag as present with ``push_media`` (default docker v2) so the post-push shape
    check reads what the build produced.
    """

    def __init__(
        self,
        *,
        existing: set[str] | None = None,
        other_error: bool = False,
        media: dict[str, str] | None = None,
        push_media: str = DOCKER_V2,
    ) -> None:
        self.calls: list[list[str]] = []
        self.existing = set(existing or set())
        self.other_error = other_error
        self.media = dict(media or {})
        self.push_media = push_media

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if "describe-images" in cmd:
            tag = next(c.split("=", 1)[1] for c in cmd if c.startswith("imageTag="))
            if self.other_error:
                raise images.RunError(cmd, 255, "", "AccessDeniedException: nope")
            if tag in self.existing:
                detail = {
                    "imageDetails": [
                        {
                            "imageDigest": f"sha256:{tag[:12]}",
                            "imageManifestMediaType": self.media.get(tag, DOCKER_V2),
                        }
                    ]
                }
                return subprocess.CompletedProcess(cmd, 0, json.dumps(detail), "")
            raise images.RunError(cmd, 254, "", "An error occurred (ImageNotFoundException) ...")
        if "imagetools" in cmd and "inspect" in cmd:
            ref = cmd[cmd.index("inspect") + 1]
            tag = ref.rsplit(":", 1)[1]
            if tag not in self.existing:
                raise images.RunError(cmd, 1, "", f"ERROR: {ref}: not found")
            body = {"mediaType": self.media.get(tag, DOCKER_V2), "digest": f"sha256:{tag[:12]}"}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")
        if "buildx" in cmd and "build" in cmd:
            for arg in cmd:
                if arg.startswith("type=image,") and "push=true" in arg:
                    name = next(p.split("=", 1)[1] for p in arg.split(",") if p.startswith("name="))
                    tag = name.rsplit(":", 1)[1]
                    self.existing.add(tag)
                    self.media[tag] = self.push_media
        return subprocess.CompletedProcess(cmd, 0, "", "")

    @property
    def build_calls(self) -> list[list[str]]:
        return [c for c in self.calls if "buildx" in c and "build" in c]


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def test_list_outputs_one_json_line_per_image(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    assert images.main(["list", "--cdk-out", str(cdk_out), "--config", str(cfg)]) == 0
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    by_hash = {ln["hash"]: ln for ln in lines}
    assert set(by_hash) == {A_HASH, B_HASH}
    a = by_hash[A_HASH]
    assert a["stack"] == "SvcA-dev"
    assert a["asset_id"] == A_HASH
    assert a["directory"] == f"asset.{A_HASH}"
    assert a["dockerfile"] == A_DOCKERFILE
    assert a["platform"] == "linux/arm64"
    assert a["build_args"] == {"CODEARTIFACT_INDEX_HOST": HOST}
    assert a["build_secrets"] == {"ca_token": "env=CODEARTIFACT_AUTH_TOKEN"}
    assert a["repository"] == REPO


def test_list_zero_images_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cdk_out = tmp_path / "cdk.out"
    cdk_out.mkdir()
    (cdk_out / "Network-dev.assets.json").write_text(
        json.dumps({"version": "54.0.0", "files": {}, "dockerImages": {}})
    )
    cfg = _write_config(tmp_path)
    assert images.main(["list", "--cdk-out", str(cdk_out), "--config", str(cfg)]) == 1
    assert "no docker image assets" in capsys.readouterr().err.lower()


def test_list_multi_destination_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dests = {
        "one": {"repositoryName": REPO, "imageTag": A_HASH, "region": REGION},
        "two": {"repositoryName": REPO, "imageTag": A_HASH, "region": "us-west-2"},
    }
    manifests = {"SvcA-dev": _image_manifest(A_HASH, A_DOCKERFILE, destinations=dests)}
    cdk_out = _write_cdk_out(tmp_path, manifests)
    cfg = _write_config(tmp_path)
    assert images.main(["list", "--cdk-out", str(cdk_out), "--config", str(cfg)]) == 1
    assert A_HASH in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #


def _build_argv(cdk_out: Path, cfg: Path, *extra: str) -> list[str]:
    return ["build", "--cdk-out", str(cdk_out), "--config", str(cfg), *extra]


def test_build_push_readwrite_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(images, "_run", rec)
    rc = images.main(
        _build_argv(cdk_out, cfg, "--registry", REGISTRY, "--mode", "push", "--cache", "readwrite")
    )
    assert rc == 0
    a_build = next(c for c in rec.build_calls if any(A_HASH in a for a in c))
    flat = " ".join(a_build)
    assert "buildx build" in flat
    assert f"--file {cdk_out}/asset.{A_HASH}/{A_DOCKERFILE}" in flat
    assert "--platform linux/arm64" in flat
    assert "--build-arg CODEARTIFACT_INDEX_HOST=" + HOST in flat
    assert "--secret id=ca_token,env=CODEARTIFACT_AUTH_TOKEN" in flat
    assert f"--cache-from type=registry,ref={A_REF}" in flat
    assert (
        f"--cache-to type=registry,ref={A_REF},mode=max,image-manifest=true,oci-mediatypes=true"
    ) in flat
    assert "--provenance=false" in a_build
    assert "--sbom=false" in a_build
    assert (f"type=image,name={REGISTRY}:{A_HASH},push=true,oci-mediatypes=false") in a_build
    assert "--push" not in a_build
    assert str(cdk_out / f"asset.{A_HASH}") == a_build[-1]
    # the svc-b asset uses the svc-b cache ref
    b_build = next(c for c in rec.build_calls if any(B_HASH in a for a in c))
    assert f"type=registry,ref={B_REF}" in " ".join(b_build)


def test_build_load_read_no_cache_to_no_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(images, "_run", rec)
    rc = images.main(
        _build_argv(cdk_out, cfg, "--registry", "none", "--mode", "load", "--cache", "read")
    )
    assert rc == 0
    for call in rec.build_calls:
        flat = " ".join(call)
        assert "--cache-from type=registry" in flat
        assert "--cache-to" not in flat
        assert "--push" not in call
        assert "--load" in call
        assert "--tag" not in call


def test_build_load_ignores_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(images, "_run", rec)
    # --registry omitted entirely is fine in load mode.
    rc = images.main(_build_argv(cdk_out, cfg, "--mode", "load", "--cache", "none"))
    assert rc == 0
    assert len(rec.build_calls) == 2


def test_build_skip_existing_skips_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder(existing={A_HASH, B_HASH})
    monkeypatch.setattr(images, "_run", rec)
    rc = images.main(
        _build_argv(
            cdk_out,
            cfg,
            "--registry",
            REGISTRY,
            "--mode",
            "push",
            "--cache",
            "none",
            "--skip-existing",
        )
    )
    assert rc == 0
    assert rec.build_calls == []  # both skipped
    assert any("describe-images" in c for c in rec.calls)


def test_build_skip_existing_builds_on_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder(existing=set())  # nothing present -> ImageNotFoundException
    monkeypatch.setattr(images, "_run", rec)
    rc = images.main(
        _build_argv(
            cdk_out,
            cfg,
            "--registry",
            REGISTRY,
            "--mode",
            "push",
            "--cache",
            "none",
            "--skip-existing",
        )
    )
    assert rc == 0
    assert len(rec.build_calls) == 2  # both built


def test_build_unknown_dockerfile_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifests = {"Mystery-dev": _image_manifest(A_HASH, "services/mystery/Dockerfile")}
    cdk_out = _write_cdk_out(tmp_path, manifests)
    cfg = _write_config(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(images, "_run", rec)
    rc = images.main(_build_argv(cdk_out, cfg, "--mode", "load", "--cache", "none"))
    assert rc == 1
    assert "mystery" in capsys.readouterr().err.lower()
    assert rec.build_calls == []


# --------------------------------------------------------------------------- #
# assert-present
# --------------------------------------------------------------------------- #


def test_assert_present_all_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder(existing={A_HASH, B_HASH})
    monkeypatch.setattr(images, "_run", rec)
    rc = images.main(
        [
            "assert-present",
            "--cdk-out",
            str(cdk_out),
            "--config",
            str(cfg),
            "--account",
            DEV_ACCOUNT,
        ]
    )
    assert rc == 0
    assert all(
        "--registry-id" in c and DEV_ACCOUNT in c for c in rec.calls if "describe-images" in c
    )


def test_assert_present_missing_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder(existing={A_HASH})  # B missing
    monkeypatch.setattr(images, "_run", rec)
    rc = images.main(
        [
            "assert-present",
            "--cdk-out",
            str(cdk_out),
            "--config",
            str(cfg),
            "--account",
            DEV_ACCOUNT,
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert B_HASH in err
    assert "SvcB-dev" in err
    assert A_HASH not in err


def test_assert_present_zero_assets_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = tmp_path / "cdk.out"
    cdk_out.mkdir()
    (cdk_out / "Network-dev.assets.json").write_text(
        json.dumps({"version": "54.0.0", "files": {}, "dockerImages": {}})
    )
    cfg = _write_config(tmp_path)
    rc = images.main(
        [
            "assert-present",
            "--cdk-out",
            str(cdk_out),
            "--config",
            str(cfg),
            "--account",
            DEV_ACCOUNT,
        ]
    )
    assert rc == 1


def test_assert_present_other_error_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder(other_error=True)
    monkeypatch.setattr(images, "_run", rec)
    with pytest.raises(images.RunError):
        images.main(
            [
                "assert-present",
                "--cdk-out",
                str(cdk_out),
                "--config",
                str(cfg),
                "--account",
                DEV_ACCOUNT,
            ]
        )


# --------------------------------------------------------------------------- #
# digests
# --------------------------------------------------------------------------- #


def test_digests_outputs_hash_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder(existing={A_HASH, B_HASH})
    monkeypatch.setattr(images, "_run", rec)
    rc = images.main(
        ["digests", "--cdk-out", str(cdk_out), "--config", str(cfg), "--account", DEV_ACCOUNT]
    )
    assert rc == 0
    rows = {
        json.loads(ln)["hash"]: json.loads(ln)["digest"]
        for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    }
    assert rows[A_HASH] == f"sha256:{A_HASH[:12]}"
    assert rows[B_HASH] == f"sha256:{B_HASH[:12]}"


# --------------------------------------------------------------------------- #
# promote (dev ECR -> prod ECR by digest)
# --------------------------------------------------------------------------- #

DEV_REPO = f"cdk-{QUALIFIER}-container-assets-{DEV_ACCOUNT}-{REGION}"
PROD_REPO = f"cdk-{QUALIFIER}-container-assets-{PROD_ACCOUNT}-{REGION}"
DEV_REGISTRY = f"{DEV_ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{DEV_REPO}"
PROD_REGISTRY = f"{PROD_ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{PROD_REPO}"


class _PromoteRun:
    """A fake ``_run`` for promote at the *registry* level (no AWS ECR API)."""

    def __init__(
        self,
        dev: dict[str, str],
        prod: dict[str, str] | None = None,
        *,
        copy_mangles: str | None = None,
        copy_media_type: str | None = None,
        dev_media: dict[str, str] | None = None,
        prod_media: dict[str, str] | None = None,
        other_error: bool = False,
        dev_registry: str = DEV_REGISTRY,
        prod_registry: str = PROD_REGISTRY,
    ) -> None:
        self.dev = dev
        self.prod = dict(prod or {})
        self.copy_mangles = copy_mangles
        self.copy_media_type = copy_media_type
        self.dev_media = dict(dev_media or {})
        self.prod_media = dict(prod_media or {})
        self.other_error = other_error
        self.dev_registry = dev_registry
        self.prod_registry = prod_registry
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if "inspect" in cmd:
            ref = cmd[cmd.index("inspect") + 1]
            if self.other_error:
                raise images.RunError(cmd, 1, "", "ERROR: failed to authorize: unexpected status")
            registry, hash_ = ref.rsplit(":", 1)
            if registry == self.dev_registry:
                table, media = self.dev, self.dev_media
            else:
                table, media = self.prod, self.prod_media
            digest = table.get(hash_)
            if digest is None:
                raise images.RunError(cmd, 1, "", f"ERROR: {ref}: not found")
            body = json.dumps(
                {"mediaType": media.get(hash_, DOCKER_V2), "digest": digest, "size": 42}
            )
            return subprocess.CompletedProcess(cmd, 0, body, "")
        if "create" in cmd:
            tag_arg = cmd[cmd.index("--tag") + 1]
            hash_ = tag_arg.rsplit(":", 1)[1]
            self.prod[hash_] = self.copy_mangles or self.dev[hash_]
            self.prod_media[hash_] = self.copy_media_type or self.dev_media.get(hash_, DOCKER_V2)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    @property
    def copy_calls(self) -> list[list[str]]:
        return [c for c in self.calls if "create" in c]


def _promote_argv(cdk_out: Path, cfg: Path, *extra: str) -> list[str]:
    return ["promote", "--cdk-out", str(cdk_out), "--config", str(cfg), *extra]


def test_promote_copies_by_digest_with_prefer_index_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _PromoteRun(dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"})
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_promote_argv(cdk_out, cfg)) == 0
    a_copy = next(c for c in rec.copy_calls if any(A_HASH in a for a in c))
    flat = " ".join(a_copy)
    assert "buildx imagetools create" in flat
    assert "--prefer-index=false" in a_copy
    assert f"--tag {PROD_REGISTRY}:{A_HASH}" in flat
    assert f"{DEV_REGISTRY}@sha256:aaa" == a_copy[-1]
    assert len(rec.copy_calls) == 2


def test_promote_missing_in_dev_exits_1_no_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _PromoteRun(dev={A_HASH: "sha256:aaa"})  # B missing in dev
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_promote_argv(cdk_out, cfg)) == 1
    err = capsys.readouterr().err
    assert B_HASH in err
    assert "SvcB-dev" in err
    assert all(B_HASH not in " ".join(c) for c in rec.copy_calls)


def test_promote_prod_same_digest_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _PromoteRun(
        dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"},
        prod={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"},
    )
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_promote_argv(cdk_out, cfg)) == 0
    assert rec.copy_calls == []
    assert "skip-present" in capsys.readouterr().err


def test_promote_prod_different_digest_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    # SvcA sorts first; put the mismatch there so the loud failure precedes any copy.
    rec = _PromoteRun(
        dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"},
        prod={A_HASH: "sha256:DIFFERENT"},
    )
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_promote_argv(cdk_out, cfg)) == 1
    err = capsys.readouterr().err
    assert A_HASH in err
    assert "built elsewhere" in err
    assert rec.copy_calls == []


def test_promote_post_copy_digest_mismatch_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _PromoteRun(dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"}, copy_mangles="sha256:WRONG")
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_promote_argv(cdk_out, cfg)) == 1
    assert "copied digest sha256:WRONG" in capsys.readouterr().err


def test_promote_post_copy_media_type_mismatch_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _PromoteRun(
        dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"}, copy_media_type=OCI_IMAGE_INDEX
    )
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_promote_argv(cdk_out, cfg)) == 1
    err = capsys.readouterr().err
    assert "media type" in err.lower()
    assert OCI_IMAGE_INDEX in err


def test_promote_reads_registry_not_ecr_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _PromoteRun(dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"})
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_promote_argv(cdk_out, cfg)) == 0
    assert all("describe-images" not in " ".join(c) for c in rec.calls)
    inspects = [c for c in rec.calls if "inspect" in c]
    assert all(c[:4] == ["docker", "buildx", "imagetools", "inspect"] for c in inspects)


def test_promote_inspect_other_error_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _PromoteRun(dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"}, other_error=True)
    monkeypatch.setattr(images, "_run", rec)
    with pytest.raises(images.RunError):
        images.main(_promote_argv(cdk_out, cfg))


def test_registry_digest_tag_absent_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = f"{DEV_REGISTRY}:{A_HASH}"

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise images.RunError(cmd, 1, "", f"ERROR: {ref}: not found")

    monkeypatch.setattr(images, "_run", fake_run)
    assert images._registry_digest(ref) is None


def test_registry_digest_other_not_found_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = f"{DEV_REGISTRY}:{A_HASH}"
    other = f"{DEV_REGISTRY}-typo"

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise images.RunError(cmd, 1, "", f"ERROR: {other}: not found")

    monkeypatch.setattr(images, "_run", fake_run)
    with pytest.raises(images.RunError):
        images._registry_digest(ref)


def test_promote_emits_dev_digest_map_to_stdout_and_github_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _PromoteRun(dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"})
    monkeypatch.setattr(images, "_run", rec)
    gh_out = tmp_path / "gh_output"
    assert images.main(_promote_argv(cdk_out, cfg, "--github-output", str(gh_out))) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0]) == {A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"}
    content = gh_out.read_text().strip()
    assert content.startswith("dev-digests=")
    assert json.loads(content[len("dev-digests=") :]) == {
        A_HASH: "sha256:aaa",
        B_HASH: "sha256:bbb",
    }


def test_promote_failure_writes_nothing_to_github_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _PromoteRun(dev={A_HASH: "sha256:aaa"})
    monkeypatch.setattr(images, "_run", rec)
    gh_out = tmp_path / "gh_output"
    assert images.main(_promote_argv(cdk_out, cfg, "--github-output", str(gh_out))) == 1
    assert not gh_out.exists()


def test_promote_reads_dev_bootstrap_repo_for_prod_synth_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _prod_dests(asset_id: str) -> dict[str, Any]:
        return {
            f"{PROD_ACCOUNT}-{REGION}-x": {
                "repositoryName": PROD_REPO,
                "imageTag": asset_id,
                "region": REGION,
            }
        }

    manifests = {
        "SvcA-prod": _image_manifest(A_HASH, A_DOCKERFILE, destinations=_prod_dests(A_HASH)),
        "SvcB-prod": _image_manifest(B_HASH, B_DOCKERFILE, destinations=_prod_dests(B_HASH)),
    }
    cdk_out = _write_cdk_out(tmp_path, manifests)
    cfg = _write_config(tmp_path)
    rec = _PromoteRun(dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"})
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_promote_argv(cdk_out, cfg)) == 0
    inspect_refs = [c[c.index("inspect") + 1] for c in rec.calls if "inspect" in c]
    dev_refs = [ref for ref in inspect_refs if ref.startswith(f"{DEV_REGISTRY}:")]
    assert f"{DEV_REGISTRY}:{A_HASH}" in dev_refs
    assert f"{DEV_REGISTRY}:{B_HASH}" in dev_refs
    assert all(PROD_REPO not in ref for ref in dev_refs)
    for copy in rec.copy_calls:
        assert copy[-1].startswith(f"{DEV_REGISTRY}@")


# --------------------------------------------------------------------------- #
# check-deployed (prod Lambda runs dev's digest)
# --------------------------------------------------------------------------- #

A_FN_LOGICAL = "SvcAFunctionF370A1F8"
B1_FN_LOGICAL = "SvcBBrokerFunctionDBA8656F"
B2_FN_LOGICAL = "SvcBReconcilerFunction3E5001DF"


def _lambda_resource(hash_: str) -> dict[str, Any]:
    return {
        "Type": "AWS::Lambda::Function",
        "Properties": {
            "Code": {
                "ImageUri": {
                    "Fn::Sub": (
                        f"{PROD_ACCOUNT}.dkr.ecr.{REGION}.${{AWS::URLSuffix}}/{PROD_REPO}:{hash_}"
                    )
                }
            }
        },
    }


def _write_prod_templates(tmp_path: Path) -> Path:
    cdk_out = tmp_path / "cdk.out"
    cdk_out.mkdir()
    (cdk_out / "SvcA-prod.template.json").write_text(
        json.dumps({"Resources": {A_FN_LOGICAL: _lambda_resource(A_HASH)}})
    )
    (cdk_out / "SvcB-prod.template.json").write_text(
        json.dumps(
            {
                "Resources": {
                    B1_FN_LOGICAL: _lambda_resource(B_HASH),
                    B2_FN_LOGICAL: _lambda_resource(B_HASH),
                }
            }
        )
    )
    return cdk_out


class _DeployedRun:
    """A fake ``_run`` for check-deployed: describe-stack-resource -> physical
    name, lambda get-function -> ResolvedImageUri whose digest comes from the
    per-hash ``running`` table (keyed by asset hash)."""

    def __init__(self, running: dict[str, str], logical_to_hash: dict[str, str]) -> None:
        self.running = running
        self.logical_to_hash = logical_to_hash
        self.calls: list[list[str]] = []
        self._by_physical: dict[str, str] = {}

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if "describe-stack-resource" in cmd:
            logical = cmd[cmd.index("--logical-resource-id") + 1]
            physical = f"phys-{logical}"
            self._by_physical[physical] = self.logical_to_hash[logical]
            detail = {"StackResourceDetail": {"PhysicalResourceId": physical}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(detail), "")
        if "get-function" in cmd:
            physical = cmd[cmd.index("--function-name") + 1]
            digest = self.running[self._by_physical[physical]]
            body = {"Code": {"ResolvedImageUri": f"{PROD_REGISTRY}@{digest}"}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _check_argv(cdk_out: Path, cfg: Path, function_map: dict[str, str]) -> list[str]:
    return [
        "check-deployed",
        "--cdk-out",
        str(cdk_out),
        "--config",
        str(cfg),
        "--function-map",
        json.dumps(function_map),
    ]


_LAMBDA_LOGICAL_TO_HASH = {
    A_FN_LOGICAL: A_HASH,
    B1_FN_LOGICAL: B_HASH,
    B2_FN_LOGICAL: B_HASH,
}


def test_check_deployed_all_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_prod_templates(tmp_path)
    cfg = _write_config(tmp_path)
    dev_digests = {A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"}
    rec = _DeployedRun(running=dict(dev_digests), logical_to_hash=_LAMBDA_LOGICAL_TO_HASH)
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_check_argv(cdk_out, cfg, dev_digests)) == 0
    out = capsys.readouterr().err
    assert "runs dev digest sha256:aaa" in out
    assert "runs dev digest sha256:bbb" in out


def test_check_deployed_mismatch_exits_1_per_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_prod_templates(tmp_path)
    cfg = _write_config(tmp_path)
    dev_digests = {A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"}
    running = {A_HASH: "sha256:aaa", B_HASH: "sha256:STALE"}
    rec = _DeployedRun(running=running, logical_to_hash=_LAMBDA_LOGICAL_TO_HASH)
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_check_argv(cdk_out, cfg, dev_digests)) == 1
    err = capsys.readouterr().err
    assert B1_FN_LOGICAL in err
    assert B2_FN_LOGICAL in err
    assert "sha256:STALE" in err
    assert "expected dev digest sha256:bbb" in err
    assert f"{A_FN_LOGICAL} (phys-{A_FN_LOGICAL}) runs dev digest sha256:aaa" in err
    assert "2 container resource(s) do not run the dev digest" in err


# --------------------------------------------------------------------------- #
# Lambda-deployable image shape
# --------------------------------------------------------------------------- #


def _push_argv(cdk_out: Path, cfg: Path, *extra: str) -> list[str]:
    return _build_argv(
        cdk_out, cfg, "--registry", REGISTRY, "--mode", "push", "--cache", "none", *extra
    )


def test_build_load_mode_also_disables_attestations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_build_argv(cdk_out, cfg, "--mode", "load", "--cache", "none")) == 0
    for call in rec.build_calls:
        assert "--provenance=false" in call
        assert "--sbom=false" in call


def test_build_push_verifies_pushed_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_push_argv(cdk_out, cfg)) == 0
    inspected = [c[c.index("inspect") + 1] for c in rec.calls if "inspect" in c]
    assert f"{REGISTRY}:{A_HASH}" in inspected
    assert f"{REGISTRY}:{B_HASH}" in inspected


def test_build_push_that_lands_as_index_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder(push_media=OCI_IMAGE_INDEX)
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_push_argv(cdk_out, cfg)) == 1
    err = capsys.readouterr().err
    assert OCI_IMAGE_INDEX in err
    assert "lambda" in err.lower()


def test_skip_existing_rejects_existing_index_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder(existing={A_HASH, B_HASH}, media={A_HASH: OCI_IMAGE_INDEX})
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_push_argv(cdk_out, cfg, "--skip-existing")) == 1
    err = capsys.readouterr().err
    assert A_HASH in err
    assert OCI_IMAGE_INDEX in err
    assert "batch-delete-image" in err
    assert rec.build_calls == []


def test_skip_existing_skips_good_existing_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder(existing={A_HASH, B_HASH})
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_push_argv(cdk_out, cfg, "--skip-existing")) == 0
    assert rec.build_calls == []


def test_assert_present_rejects_index_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder(existing={A_HASH, B_HASH}, media={A_HASH: OCI_IMAGE_INDEX})
    monkeypatch.setattr(images, "_run", rec)
    rc = images.main(
        [
            "assert-present",
            "--cdk-out",
            str(cdk_out),
            "--config",
            str(cfg),
            "--account",
            DEV_ACCOUNT,
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert A_HASH in err
    assert OCI_IMAGE_INDEX in err


def test_assert_present_accepts_oci_image_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder(existing={A_HASH, B_HASH}, media={A_HASH: OCI_IMAGE_MANIFEST})
    monkeypatch.setattr(images, "_run", rec)
    rc = images.main(
        [
            "assert-present",
            "--cdk-out",
            str(cdk_out),
            "--config",
            str(cfg),
            "--account",
            DEV_ACCOUNT,
        ]
    )
    assert rc == 0


def test_promote_refuses_index_source_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _PromoteRun(
        dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"}, dev_media={A_HASH: OCI_IMAGE_INDEX}
    )
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_promote_argv(cdk_out, cfg)) == 1
    assert rec.copy_calls == []
    err = capsys.readouterr().err
    assert OCI_IMAGE_INDEX in err
    assert "lambda" in err.lower()


# --------------------------------------------------------------------------- #
# New: config-driven behavior (Task 2, Step 2)
# --------------------------------------------------------------------------- #


def test_image_key_from_config(tmp_path: Path) -> None:
    cfg = images._load_config(_write_config(tmp_path))
    # a matching (Dockerfile, target) resolves to its configured key + policy
    entry = cfg.entry_for(A_DOCKERFILE, "")
    assert entry.key == "svc-a"
    assert entry.deploy_target == "lambda"
    # a suffix match works too (the manifest's dockerFile may carry a prefix)
    assert cfg.entry_for("cdk.out/staging/" + A_DOCKERFILE, "").key == "svc-a"
    # an asset matching no [[image]] entry raises and names the Dockerfile
    with pytest.raises(images.ConfigError) as excinfo:
        cfg.entry_for("services/mystery/Dockerfile", "")
    assert "services/mystery/Dockerfile" in str(excinfo.value)


def test_cache_ref_derived_with_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_build_argv(cdk_out, cfg, "--mode", "load", "--cache", "read")) == 0
    a_build = next(c for c in rec.build_calls if any(A_HASH in a for a in c))
    # <cache_registry>/<cache_repo>:<cache_prefix>-<key>
    assert f"--cache-from type=registry,ref={A_REF}" in " ".join(a_build)
    assert A_REF.startswith(f"{CACHE_REGISTRY}/{CACHE_REPO}:")
    assert A_REF.endswith(f":{CACHE_PREFIX}-svc-a")


def test_cache_repo_empty_means_no_cache_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cdk_out = _write_cdk_out(tmp_path)
    cfg = _write_config(tmp_path, cache_repo="")
    rec = _Recorder()
    monkeypatch.setattr(images, "_run", rec)
    # --cache read with an empty cache_repo must fail loudly, not build uncached.
    assert images.main(_build_argv(cdk_out, cfg, "--mode", "load", "--cache", "read")) == 1
    err = capsys.readouterr().err
    assert "cache_repo" in err
    assert rec.build_calls == []
    # --cache none is still fine with an empty cache_repo.
    rec2 = _Recorder()
    monkeypatch.setattr(images, "_run", rec2)
    assert images.main(_build_argv(cdk_out, cfg, "--mode", "load", "--cache", "none")) == 0
    for call in rec2.build_calls:
        assert "--cache-from" not in call


def test_missing_config_key_raises_naming_it(tmp_path: Path) -> None:
    # A missing top-level key raises ConfigError naming the exact key.
    cfg = tmp_path / "images.toml"
    cfg.write_text(_config_text(drop="region"))
    with pytest.raises(images.ConfigError) as excinfo:
        images._load_config(cfg)
    assert "region" in str(excinfo.value)
    # A missing nested key names its dotted path.
    cfg.write_text(_config_text(drop="accounts.prod"))
    with pytest.raises(images.ConfigError) as excinfo:
        images._load_config(cfg)
    assert "accounts.prod" in str(excinfo.value)
    # Surfaced through a command as a named FAIL (rc 1), never swallowed.
    cfg.write_text(_config_text(drop="cache_prefix"))
    cdk_out = _write_cdk_out(tmp_path)
    rc = images.main(
        [
            "assert-present",
            "--cdk-out",
            str(cdk_out),
            "--config",
            str(cfg),
            "--account",
            DEV_ACCOUNT,
        ]
    )
    assert rc == 1


def test_region_and_qualifier_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    region = "eu-west-2"
    qualifier = "custom01 qual".replace(" ", "")  # keep a valid single token
    cfg = _write_config(tmp_path, region=region, qualifier=qualifier)
    cdk_out = _write_cdk_out(tmp_path)
    dev_repo = f"cdk-{qualifier}-container-assets-{DEV_ACCOUNT}-{region}"
    prod_repo = f"cdk-{qualifier}-container-assets-{PROD_ACCOUNT}-{region}"
    dev_registry = f"{DEV_ACCOUNT}.dkr.ecr.{region}.amazonaws.com/{dev_repo}"
    prod_registry = f"{PROD_ACCOUNT}.dkr.ecr.{region}.amazonaws.com/{prod_repo}"
    rec = _PromoteRun(
        dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"},
        dev_registry=dev_registry,
        prod_registry=prod_registry,
    )
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_promote_argv(cdk_out, cfg)) == 0
    inspect_refs = [c[c.index("inspect") + 1] for c in rec.calls if "inspect" in c]
    # every read used the region + qualifier from config
    assert any(ref.startswith(f"{dev_registry}:") for ref in inspect_refs)
    assert all(f".dkr.ecr.{region}.amazonaws.com" in ref for ref in inspect_refs)
    assert all(f"cdk-{qualifier}-" in ref for ref in inspect_refs)


# --------------------------------------------------------------------------- #
# New: Batch awareness (Task 2, Step 2)
# --------------------------------------------------------------------------- #

BATCH_DOCKERFILE = "services/batch/Dockerfile"
BATCH_IMAGES = [
    {"key": "svc-a", "dockerfile": A_DOCKERFILE, "target": "", "deploy_target": "lambda"},
    {"key": "batch", "dockerfile": BATCH_DOCKERFILE, "target": "app", "deploy_target": "batch"},
]


def test_batch_target_skips_lambda_media_type_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A single batch asset pushed as an OCI index is accepted (Batch has no
    # single-manifest rule); the same shape is rejected for a lambda asset.
    cfg = _write_config(tmp_path, images_rows=BATCH_IMAGES)
    batch_manifests = {"Batch-dev": _image_manifest(A_HASH, BATCH_DOCKERFILE, target="app")}
    cdk_out = _write_cdk_out(tmp_path, batch_manifests)
    rec = _Recorder(push_media=OCI_IMAGE_INDEX)
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_push_argv(cdk_out, cfg)) == 0  # batch index accepted
    # A lambda asset with the very same index shape is rejected.
    lambda_manifests = {"SvcA-dev": _image_manifest(A_HASH, A_DOCKERFILE)}
    cdk_out2 = _write_cdk_out(tmp_path / "lambda", lambda_manifests)
    rec2 = _Recorder(push_media=OCI_IMAGE_INDEX)
    monkeypatch.setattr(images, "_run", rec2)
    assert images.main(_push_argv(cdk_out2, cfg)) == 1
    err = capsys.readouterr().err
    assert OCI_IMAGE_INDEX in err
    assert "lambda" in err.lower()


BATCH_HASH = "b1b1b1b1c2c2c2c2d3d3d3d3e4e4e4e4f5f5f5f5a6a6a6a6b7b7b7b7c8c8c8c8"
BATCH_JOBS = ["JobString1A", "JobSub2B", "JobJoin3C"]
FIXTURES_CDK_OUT = Path(__file__).parent / "fixtures" / "cdk_out"


class _BatchDeployedRun:
    """A fake ``_run`` for check-deployed against Batch job definitions.

    ``describe-stack-resource`` -> a job-definition ARN; ``batch
    describe-job-definitions`` -> a containerProperties.image whose ``@<digest>``
    comes from ``running`` (keyed by job-definition physical name), or no digest
    when ``unresolvable`` names it.
    """

    def __init__(self, running: dict[str, str], *, unresolvable: set[str] | None = None) -> None:
        self.running = running
        self.unresolvable = set(unresolvable or set())
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if "describe-stack-resource" in cmd:
            logical = cmd[cmd.index("--logical-resource-id") + 1]
            arn = f"arn:aws:batch:{REGION}:{PROD_ACCOUNT}:job-definition/{logical}:1"
            detail = {"StackResourceDetail": {"PhysicalResourceId": arn}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(detail), "")
        if "describe-job-definitions" in cmd:
            arn = cmd[cmd.index("--job-definitions") + 1]
            logical = arn.rsplit("/", 1)[1].rsplit(":", 1)[0]
            if logical in self.unresolvable:
                image = f"{PROD_REGISTRY}:{BATCH_HASH}"  # a tag, no @digest
            else:
                image = f"{PROD_REGISTRY}@{self.running[logical]}"
            body = {"jobDefinitions": [{"containerProperties": {"image": image}}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_check_deployed_batch_job_definition_ok_and_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Uses the static Batch fixture: three job definitions whose images are a
    # plain string, an Fn::Sub and an Fn::Join, all of the same asset hash.
    cdk_out = tmp_path / "cdk.out"
    cdk_out.mkdir()
    (cdk_out / "Batch-prod.template.json").write_text(
        (FIXTURES_CDK_OUT / "batch" / "Batch-prod.template.json").read_text()
    )
    cfg = _write_config(tmp_path, images_rows=BATCH_IMAGES)
    dev_digests = {BATCH_HASH: "sha256:batch"}

    # ok: every job definition runs the dev digest (all three shapes resolve).
    rec = _BatchDeployedRun(running={j: "sha256:batch" for j in BATCH_JOBS})
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_check_argv(cdk_out, cfg, dev_digests)) == 0
    out = capsys.readouterr().err
    for job in BATCH_JOBS:
        assert job in out

    # mismatch: one job definition runs a stale digest -> FAIL naming it.
    running = {j: "sha256:batch" for j in BATCH_JOBS}
    running["JobJoin3C"] = "sha256:STALE"
    rec2 = _BatchDeployedRun(running=running)
    monkeypatch.setattr(images, "_run", rec2)
    assert images.main(_check_argv(cdk_out, cfg, dev_digests)) == 1
    err = capsys.readouterr().err
    assert "JobJoin3C" in err
    assert "sha256:STALE" in err

    # unresolvable: a job definition whose deployed image carries no digest ->
    # FAIL naming that job definition.
    rec3 = _BatchDeployedRun(
        running={j: "sha256:batch" for j in BATCH_JOBS}, unresolvable={"JobSub2B"}
    )
    monkeypatch.setattr(images, "_run", rec3)
    assert images.main(_check_argv(cdk_out, cfg, dev_digests)) == 1
    err = capsys.readouterr().err
    assert "JobSub2B" in err
    assert "no resolved image digest" in err


def test_check_deployed_lambda_fixture_hash_extraction(tmp_path: Path) -> None:
    # The static Lambda fixture's Fn::Sub ImageUri resolves to its asset hash.
    lambda_hash = "a1a1a1a1b2b2b2b2c3c3c3c3d4d4d4d4e5e5e5e5f6f6f6f6a7a7a7a7b8b8b8b8"
    template = json.loads(
        (FIXTURES_CDK_OUT / "lambda" / "SvcLambda-prod.template.json").read_text()
    )
    image_uri = template["Resources"]["SvcFunction0A1B2C3D"]["Properties"]["Code"]["ImageUri"]
    assert images._hash_from_image_uri(image_uri) == lambda_hash


# --------------------------------------------------------------------------- #
# New: single-account promote (Task 2, Step 2)
# --------------------------------------------------------------------------- #


def test_single_account_promote_is_noop_equal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # dev == prod: promote verifies each source is present and copies nothing.
    cfg = _write_config(tmp_path, prod=DEV_ACCOUNT)
    cdk_out = _write_cdk_out(tmp_path)
    single_registry = DEV_REGISTRY  # prod registry == dev registry when accounts equal
    rec = _PromoteRun(
        dev={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"},
        prod={A_HASH: "sha256:aaa", B_HASH: "sha256:bbb"},
        dev_registry=single_registry,
        prod_registry=single_registry,
    )
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_promote_argv(cdk_out, cfg)) == 0
    assert rec.copy_calls == []
    assert "skip-present" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# New (Fix round 1): match on (Dockerfile, build target), not Dockerfile alone
# --------------------------------------------------------------------------- #

# A real-world shape: two Lambda images from ONE Dockerfile, distinguished
# only by build target; plus a Batch image on its own Dockerfile/target.
SHARED_DOCKERFILE = "infra/docker/Dockerfile.lambda"
SAME_FILE_IMAGES = [
    {"key": "light", "dockerfile": SHARED_DOCKERFILE, "target": "light", "deploy_target": "lambda"},
    {
        "key": "conform",
        "dockerfile": SHARED_DOCKERFILE,
        "target": "conform",
        "deploy_target": "lambda",
    },
    {"key": "batch", "dockerfile": BATCH_DOCKERFILE, "target": "app", "deploy_target": "batch"},
]

PROBE_HASH = "cccc1111dddd2222eeee3333ffff4444aaaa5555bbbb6666cccc7777dddd8888"


def test_same_dockerfile_different_target_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = images._load_config(_write_config(tmp_path, images_rows=SAME_FILE_IMAGES))
    # Same Dockerfile, different target -> the right entry, never first-match-wins.
    assert cfg.entry_for(SHARED_DOCKERFILE, "light").key == "light"
    assert cfg.entry_for(SHARED_DOCKERFILE, "conform").key == "conform"
    # refresh, broker and probe are three separate light assets: all resolve to
    # the single light entry (same key/cache ref/deploy policy), by suffix match.
    light = cfg.entry_for(SHARED_DOCKERFILE, "light")
    for prefixed in (
        "cdk.out/refresh/" + SHARED_DOCKERFILE,
        "cdk.out/broker/" + SHARED_DOCKERFILE,
        "cdk.out/probe/" + SHARED_DOCKERFILE,
    ):
        assert cfg.entry_for(prefixed, "light").key == light.key == "light"

    # End-to-end through `build`: three assets share the Dockerfile — two at
    # target light (refresh, broker) and one at conform — and each derives its
    # cache ref from the matched entry's key and passes the matched --target.
    cfg_path = _write_config(tmp_path, images_rows=SAME_FILE_IMAGES)
    manifests = {
        "Refresh-dev": _image_manifest(A_HASH, SHARED_DOCKERFILE, target="light"),
        "Broker-dev": _image_manifest(PROBE_HASH, SHARED_DOCKERFILE, target="light"),
        "Conform-dev": _image_manifest(B_HASH, SHARED_DOCKERFILE, target="conform"),
    }
    cdk_out = _write_cdk_out(tmp_path, manifests)
    rec = _Recorder()
    monkeypatch.setattr(images, "_run", rec)
    assert images.main(_build_argv(cdk_out, cfg_path, "--mode", "load", "--cache", "read")) == 0
    for hash_ in (A_HASH, PROBE_HASH):
        light_build = next(c for c in rec.build_calls if any(hash_ in a for a in c))
        flat = " ".join(light_build)
        assert f"{CACHE_PREFIX}-light" in flat
        assert "--target light" in flat
    conform_build = next(c for c in rec.build_calls if any(B_HASH in a for a in c))
    conform_flat = " ".join(conform_build)
    assert f"{CACHE_PREFIX}-conform" in conform_flat
    assert "--target conform" in conform_flat


def test_ambiguous_config_raises(tmp_path: Path) -> None:
    # Two entries with the SAME (Dockerfile, target) is now rejected AT LOAD
    # (deferred improvement 1: the config loader is the only validator now that
    # there is no copier). The error names both offending keys — no
    # first-match-wins.
    rows = [
        {
            "key": "one",
            "dockerfile": SHARED_DOCKERFILE,
            "target": "light",
            "deploy_target": "lambda",
        },
        {
            "key": "two",
            "dockerfile": SHARED_DOCKERFILE,
            "target": "light",
            "deploy_target": "lambda",
        },
    ]
    with pytest.raises(images.ConfigError) as excinfo:
        images._load_config(_write_config(tmp_path, images_rows=rows))
    msg = str(excinfo.value)
    assert "duplicate" in msg.lower()
    assert SHARED_DOCKERFILE in msg
    assert "one" in msg
    assert "two" in msg


def test_entry_for_ambiguous_defensive_guard() -> None:
    # entry_for keeps its own "ambiguous" guard for a directly-constructed Config
    # (a loaded config can no longer carry duplicates, but the defensive branch
    # stays and is covered here). Naming both candidate keys, no first-match.
    cfg = images.Config(
        region=REGION,
        bootstrap_qualifier=QUALIFIER,
        cache_repo=CACHE_REPO,
        cache_prefix=CACHE_PREFIX,
        dev_account=DEV_ACCOUNT,
        prod_account=PROD_ACCOUNT,
        images=(
            images.ImageEntry(
                key="one", dockerfile=SHARED_DOCKERFILE, target="light", deploy_target="lambda"
            ),
            images.ImageEntry(
                key="two", dockerfile=SHARED_DOCKERFILE, target="light", deploy_target="lambda"
            ),
        ),
    )
    with pytest.raises(images.ConfigError) as excinfo:
        cfg.entry_for(SHARED_DOCKERFILE, "light")
    msg = str(excinfo.value)
    assert "ambiguous" in msg
    assert "one" in msg
    assert "two" in msg


def test_config_rejects_bad_key_regex_and_deploy_target(tmp_path: Path) -> None:
    # Every rule the copier validators enforced now lives in the loader.
    # Bad key (underscore/uppercase) fails naming the offending key.
    bad_key = [
        {"key": "Svc_A", "dockerfile": A_DOCKERFILE, "target": "", "deploy_target": "lambda"}
    ]
    with pytest.raises(images.ConfigError) as excinfo:
        images._load_config(_write_config(tmp_path, images_rows=bad_key))
    assert "Svc_A" in str(excinfo.value)
    # Duplicate key fails naming it.
    dup_key = [
        {"key": "svc", "dockerfile": A_DOCKERFILE, "target": "", "deploy_target": "lambda"},
        {"key": "svc", "dockerfile": B_DOCKERFILE, "target": "", "deploy_target": "lambda"},
    ]
    with pytest.raises(images.ConfigError) as excinfo:
        images._load_config(_write_config(tmp_path, images_rows=dup_key))
    assert "duplicate image key" in str(excinfo.value)
    assert "svc" in str(excinfo.value)
    # deploy_target outside {lambda, batch} fails naming the value.
    bad_dt = [
        {"key": "svc", "dockerfile": A_DOCKERFILE, "target": "", "deploy_target": "fargate"}
    ]
    with pytest.raises(images.ConfigError) as excinfo:
        images._load_config(_write_config(tmp_path, images_rows=bad_dt))
    assert "fargate" in str(excinfo.value)
    # A missing required image key fails naming it.
    cfg = tmp_path / "missing.toml"
    cfg.write_text(
        _config_text(images_rows=[]).rstrip("\n")
        + '\n[[image]]\nkey = "svc"\ndockerfile = "x"\ntarget = ""\n'
    )
    with pytest.raises(images.ConfigError) as excinfo:
        images._load_config(cfg)
    assert "deploy_target" in str(excinfo.value)
    # An empty image array fails loudly.
    empty = tmp_path / "empty.toml"
    empty.write_text(_config_text(images_rows=[]))
    with pytest.raises(images.ConfigError) as excinfo:
        images._load_config(empty)
    assert "image" in str(excinfo.value)


def test_build_push_without_registry_rejected_at_parse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # deferred improvement 2: `build --mode push` without --registry is rejected
    # during argparse parsing (SystemExit code 2), so the README-snippet test
    # catches a push snippet that forgot --registry.
    parser = images._build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["build", "--cdk-out", "x", "--mode", "push", "--cache", "none"])
    assert excinfo.value.code == 2
    assert "--registry" in capsys.readouterr().err
    # --registry none is the load-mode sentinel and is likewise rejected in push.
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["build", "--cdk-out", "x", "--mode", "push", "--cache", "none", "--registry", "none"]
        )
    # load mode with no registry still parses fine.
    ns = parser.parse_args(["build", "--cdk-out", "x", "--mode", "load", "--cache", "none"])
    assert ns.mode == "load"


def test_target_mismatch_raises(tmp_path: Path) -> None:
    # An asset whose Dockerfile matches an entry but whose build target does not
    # raises naming the Dockerfile AND the target — never a suffix-only fallback.
    cfg = images._load_config(_write_config(tmp_path, images_rows=SAME_FILE_IMAGES))
    with pytest.raises(images.ConfigError) as excinfo:
        cfg.entry_for(SHARED_DOCKERFILE, "mystery")
    msg = str(excinfo.value)
    assert SHARED_DOCKERFILE in msg
    assert "mystery" in msg


def test_batch_image_of_ecs_properties_multi_container() -> None:
    # aws-cdk-lib 2.271.0's EcsJobDefinition L2 renders
    # ContainerProperties.Image (verified against the live stack; see
    # task-2-report.md) and never the L1 EcsProperties multi-container shape.
    # But check-deployed parses raw synthesized templates, so _batch_image_of
    # also handles EcsProperties.TaskProperties[].Containers[].Image
    # defensively. Cover that branch.
    image = f"{PROD_REGISTRY}:{BATCH_HASH}"
    props = {"EcsProperties": {"TaskProperties": [{"Containers": [{"Image": image}]}]}}
    assert images._batch_image_of(props) == image
    # ContainerProperties takes precedence when both are present.
    both = {"ContainerProperties": {"Image": "cp-image:tag"}, "EcsProperties": {}}
    assert images._batch_image_of(both) == "cp-image:tag"
    # Neither carries an image -> None.
    assert images._batch_image_of({"Type": "container"}) is None
