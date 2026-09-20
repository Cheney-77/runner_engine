from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import CatalogError
from .model import OperatorRelease, RuntimeEnv


class Catalog:
    """Single source for immutable operator release metadata and artifacts."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, manifest: dict[str, Any], artifact: bytes) -> OperatorRelease:
        release_id = str(manifest["id"])
        release_dir = self.root / release_id
        if release_dir.exists():
            existing = self.get(release_id)
            if existing.artifact_sha256 != manifest["artifact_sha256"]:
                raise CatalogError("CATALOG_CONFLICT", f"release {release_id} already exists with different bits")
            return existing

        release_dir.mkdir(parents=True)
        artifact_path = release_dir / "operator.tar.gz"
        metadata_path = release_dir / "release.json"

        tmp_artifact = release_dir / ".operator.tar.gz.tmp"
        tmp_metadata = release_dir / ".release.json.tmp"
        tmp_artifact.write_bytes(artifact)
        tmp_metadata.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp_artifact.replace(artifact_path)
        tmp_metadata.replace(metadata_path)
        return self.get(release_id)

    def get(self, release_id: str) -> OperatorRelease:
        path = self.root / release_id / "release.json"
        if not path.is_file():
            raise CatalogError("RELEASE_NOT_FOUND", f"unknown release {release_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        runtime_raw = raw["runtime"]
        runtime = RuntimeEnv(
            id=runtime_raw["id"],
            image=runtime_raw["image"],
            python=runtime_raw.get("python", "3.12"),
        )
        artifact_path = self.root / release_id / "operator.tar.gz"
        if not artifact_path.is_file():
            raise CatalogError("ARTIFACT_NOT_FOUND", f"artifact missing for {release_id}")
        return OperatorRelease(
            id=raw["id"],
            artifact_path=str(artifact_path),
            artifact_sha256=raw["artifact_sha256"],
            runtime=runtime,
            entrypoint=raw["entrypoint"],
            profile=raw.get("profile", "standard"),
            input_attributes=tuple(raw.get("input_attributes", [])),
            output_attributes=tuple(raw.get("output_attributes", [])),
        )
