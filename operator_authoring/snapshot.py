from __future__ import annotations

import shutil
from pathlib import Path

from .ast_scanner import EXCLUDED_DIRS


class SnapshotError(ValueError):
    pass


def snapshot_workspace(source: str | Path, destination: str | Path, *, max_bytes: int = 20 * 1024 * 1024) -> Path:
    source_root = Path(source).resolve()
    destination_root = Path(destination).resolve()

    if not source_root.is_dir():
        raise SnapshotError(f"workspace does not exist: {source_root}")
    if not (source_root / "requirements.txt").is_file():
        raise SnapshotError("workspace root must contain requirements.txt; it may be empty")

    destination_root.mkdir(parents=True, exist_ok=True)
    total = 0

    for path in sorted(source_root.rglob("*"), key=lambda item: item.relative_to(source_root).as_posix()):
        relative = path.relative_to(source_root)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            raise SnapshotError(f"symlinks are not allowed in operator workspace: {relative}")

        target = destination_root / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            continue

        size = path.stat().st_size
        total += size
        if total > max_bytes:
            raise SnapshotError(f"workspace source exceeds {max_bytes} bytes")

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    return destination_root


def read_requirements(workspace: str | Path) -> list[str]:
    path = Path(workspace) / "requirements.txt"
    if not path.is_file():
        raise SnapshotError("requirements.txt is required")

    result: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            raise SnapshotError(f"requirements.txt line {number}: line continuations are not supported")
        if line.startswith("-"):
            raise SnapshotError(
                f"requirements.txt line {number}: pip options/includes are not supported; "
                "configure package indexes in Build Service"
            )
        if " #" in line:
            line = line.split(" #", 1)[0].rstrip()
        if line:
            result.append(line)
    return result
