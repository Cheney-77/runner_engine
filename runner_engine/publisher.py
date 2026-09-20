from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
from pathlib import Path

import yaml

from .catalog import Catalog
from .model import OperatorRelease

_ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
_OCI_DIGEST = re.compile(r"^.+@sha256:[0-9a-fA-F]{64}$")
_IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".qoder"}
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _iter_source_files(root: Path):
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed in operator source: {relative}")
        yield relative, path


def _source_hash(root: Path) -> str:
    h = hashlib.sha256()
    for relative, path in _iter_source_files(root):
        h.update(relative.as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest())
        h.update(b"\0")
    return h.hexdigest()


def _deterministic_archive(root: Path, compiled_manifest: dict) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for relative, path in _iter_source_files(root):
            data = path.read_bytes()
            info = tarfile.TarInfo(relative.as_posix())
            info.size = len(data)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o444
            tar.addfile(info, io.BytesIO(data))

        manifest_bytes = _canonical(compiled_manifest)
        info = tarfile.TarInfo("operator.json")
        info.size = len(manifest_bytes)
        info.mtime = 0
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mode = 0o444
        tar.addfile(info, io.BytesIO(manifest_bytes))

    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", filename="", mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


class OperatorPublisher:
    """The one public publishing object. Parsing/validation/packing stay private."""

    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    def publish(
        self,
        project_root: str | Path,
        *,
        runtime_image: str,
        profile: str = "standard",
        python: str = "3.12",
        allow_mutable_image: bool = False,
    ) -> OperatorRelease:
        root = Path(project_root).resolve()
        spec_path = root / "operator.yaml"
        if not spec_path.is_file():
            raise ValueError(f"{spec_path} is required")

        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        if not isinstance(spec, dict):
            raise ValueError("operator.yaml must contain a mapping")

        entrypoint = str(spec.get("entrypoint", ""))
        if not _ENTRYPOINT.fullmatch(entrypoint):
            raise ValueError("entrypoint must look like 'module.submodule:function'")

        for key in ("input_attributes", "output_attributes"):
            value = spec.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise ValueError(f"{key} must be a list of non-empty strings")

        if not allow_mutable_image and not _OCI_DIGEST.fullmatch(runtime_image):
            raise ValueError("runtime_image must be pinned as repo/image@sha256:<64 hex chars>")

        # The immutable OCI image digest is the runtime truth. Dependency locks belong
        # to the build plane; duplicating them into a second public "environment digest"
        # only makes the domain model harder to understand.
        runtime_id = _sha256(runtime_image.encode("utf-8"))

        source_hash = _source_hash(root)
        identity = {
            "abi": 1,
            "runtime_id": runtime_id,
            "entrypoint": entrypoint,
            "profile": profile,
            "input_attributes": sorted(set(spec.get("input_attributes", []))),
            "output_attributes": sorted(set(spec.get("output_attributes", []))),
            "source": source_hash,
        }
        release_id = _sha256(_canonical(identity))

        compiled_manifest = {
            "abi": 1,
            "id": release_id,
            "entrypoint": entrypoint,
            "input_attributes": identity["input_attributes"],
            "output_attributes": identity["output_attributes"],
        }
        artifact = _deterministic_archive(root, compiled_manifest)
        if len(artifact) > MAX_ARTIFACT_BYTES:
            raise ValueError("compressed operator artifact exceeds 10 MiB")
        artifact_sha256 = _sha256(artifact)

        manifest = {
            "id": release_id,
            "artifact_sha256": artifact_sha256,
            "runtime": {
                "id": runtime_id,
                "image": runtime_image,
                "python": python,
            },
            "entrypoint": entrypoint,
            "profile": profile,
            "input_attributes": identity["input_attributes"],
            "output_attributes": identity["output_attributes"],
        }
        return self.catalog.save(manifest, artifact)


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish an immutable Python operator")
    parser.add_argument("project")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--profile", default="standard")
    parser.add_argument("--allow-mutable-image", action="store_true")
    args = parser.parse_args()

    release = OperatorPublisher(Catalog(args.catalog)).publish(
        args.project,
        runtime_image=args.runtime_image,
        profile=args.profile,
        allow_mutable_image=args.allow_mutable_image,
    )
    print(release.id)


if __name__ == "__main__":
    main()
