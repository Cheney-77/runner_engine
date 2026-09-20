from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from operator_authoring.compiler import canonical_json

from .backends.models import NativeTargetPlatform
from .edge_dependencies import EdgeDependencyResolution, EdgeDependencyResolver


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_edge_bundle(
    output_root: Path,
    *,
    user_id: int,
    target: NativeTargetPlatform,
    revision: int,
    members: list[dict[str, Any]],
    resolution: EdgeDependencyResolution,
    resolver: EdgeDependencyResolver,
) -> tuple[Path, str, dict[str, Any]]:
    output_root.mkdir(parents=True, exist_ok=True)

    target_key = f"{target.os}-{target.arch}-py{target.python_version}"
    target_root = output_root / f"user-{user_id}" / target_key
    target_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mpr-edge-bundle-") as tmp:
        staging = Path(tmp) / "edge-bundle"
        processors_root = staging / "processors"
        dependencies_root = staging / "dependencies"

        processors_root.mkdir(parents=True)
        dependencies_root.mkdir(parents=True)

        manifest_members = []
        for member in sorted(members, key=lambda item: (str(item["operator_id"]), str(item["variant_id"]))):
            source = Path(member["artifact_file"])
            if not source.is_file():
                raise FileNotFoundError(f"native processor artifact does not exist: {source}")

            filename = f'{member["package_name"]}-{str(member["variant_id"])[:8]}.zip'
            destination = processors_root / filename
            shutil.copy2(source, destination)

            manifest_members.append(
                {
                    "operatorId": str(member["operator_id"]),
                    "variantId": str(member["variant_id"]),
                    "packageName": member["package_name"],
                    "processorArtifact": f"processors/{filename}",
                    "requirements": list(member.get("requirements") or []),
                }
            )

        resolver.materialize(resolution, target=target, destination=dependencies_root)

        requirements_in = "\n".join(resolution.direct_requirements)
        if requirements_in:
            requirements_in += "\n"

        (staging / "requirements.in").write_text(requirements_in, encoding="utf-8")
        (staging / "requirements.lock").write_text(resolution.lock_text, encoding="utf-8")

        manifest = {
            "schema": "dsc.edge-native-bundle/v1",
            "userId": user_id,
            "revision": revision,
            "target": {
                "os": target.os,
                "arch": target.arch,
                "pythonVersion": target.python_version,
                "uvPythonPlatform": target.uv_python_platform,
            },
            "dependencyLockSha256": resolution.lock_sha256,
            "dependencyPackageCount": resolution.package_count,
            "processors": manifest_members,
        }
        (staging / "manifest.json").write_bytes(canonical_json(manifest))

        identity = {
            "manifest": manifest,
            "requirementsLockSha256": resolution.lock_sha256,
        }
        bundle_key = hashlib.sha256(canonical_json(identity)).hexdigest()
        filename = f"edge-native-{target_key}-r{revision:06d}-{bundle_key[:12]}.zip"
        final_zip = target_root / filename

        if final_zip.exists():
            final_zip.unlink()

        with zipfile.ZipFile(final_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging.parent).as_posix())

    return final_zip, _sha256_file(final_zip), manifest
