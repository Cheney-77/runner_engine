from __future__ import annotations

import hashlib
import io
import os
import shutil
import tarfile
from pathlib import Path


class ReleaseMaterializer:
    def __init__(
        self,
        root: str | Path,
        *,
        max_files: int = 2_000,
        max_expanded_bytes: int = 64 * 1024 * 1024,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

        # Child may traverse a known release path, but cannot enumerate all releases.
        try:
            self.root.chmod(0o711)
        except OSError:
            pass

        self.max_files = max_files
        self.max_expanded_bytes = max_expanded_bytes

    def install(
        self,
        release_id: str,
        expected_sha256: str,
        artifact: bytes,
    ) -> Path:
        actual = hashlib.sha256(artifact).hexdigest()
        if actual != expected_sha256:
            raise ValueError("artifact sha256 mismatch")

        target = self.root / release_id
        if target.exists():
            return target

        staging = self.root / f".{release_id}.tmp-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)

        staging.mkdir(parents=True)
        file_count = 0
        total = 0
        names: set[str] = set()

        try:
            with tarfile.open(fileobj=io.BytesIO(artifact), mode="r:*") as tar:
                for member in tar.getmembers():
                    file_count += 1
                    if file_count > self.max_files:
                        raise ValueError("artifact contains too many entries")

                    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                        raise ValueError(
                            f"special/link entry is forbidden: {member.name}"
                        )

                    pure = Path(member.name)
                    if pure.is_absolute() or ".." in pure.parts:
                        raise ValueError(f"unsafe artifact path: {member.name}")

                    normalized = pure.as_posix()
                    if normalized in names:
                        raise ValueError(f"duplicate artifact path: {member.name}")
                    names.add(normalized)

                    if member.isfile():
                        total += member.size
                        if total > self.max_expanded_bytes:
                            raise ValueError(
                                "artifact expands beyond configured limit"
                            )

                    destination = (staging / pure).resolve()
                    staging_resolved = staging.resolve()
                    if (
                        staging_resolved not in destination.parents
                        and destination != staging_resolved
                    ):
                        raise ValueError(
                            f"path escapes staging directory: {member.name}"
                        )

                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        source = tar.extractfile(member)
                        if source is None:
                            raise ValueError(f"could not read {member.name}")

                        with destination.open("wb") as out:
                            shutil.copyfileobj(source, out, length=64 * 1024)

            manifest = staging / "operator.json"
            if not manifest.is_file():
                raise ValueError("operator.json missing from release")

            self._make_read_only(staging)
            staging.replace(target)
            return target
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def _make_read_only(root: Path) -> None:
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                path.chmod(0o555)
            else:
                path.chmod(0o444)
        root.chmod(0o555)
