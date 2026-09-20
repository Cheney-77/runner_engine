from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from operator_authoring.ast_scanner import source_revision
from operator_authoring.snapshot import snapshot_workspace


class SourceStoreError(RuntimeError):
    pass


class LocalSourceStore:
    def __init__(self, root: str | Path, *, max_source_bytes: int = 20 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_source_bytes = max_source_bytes

    def put_workspace(self, workspace: str | Path, *, expected_revision: str) -> str:
        final_dir = self.root / expected_revision
        if final_dir.is_dir():
            actual = source_revision(final_dir)
            if actual != expected_revision:
                raise SourceStoreError(
                    f"source store corruption: expected {expected_revision}, found {actual}"
                )
            return f"local:{expected_revision}"

        temp_dir = Path(tempfile.mkdtemp(prefix=f".{expected_revision[:12]}-", dir=self.root))
        try:
            snapshot_workspace(workspace, temp_dir, max_bytes=self.max_source_bytes)
            actual = source_revision(temp_dir)
            if actual != expected_revision:
                raise SourceStoreError(
                    "workspace changed while creating immutable source snapshot; analyze again"
                )
            try:
                os.replace(temp_dir, final_dir)
            except OSError:
                if not final_dir.is_dir():
                    raise
            return f"local:{expected_revision}"
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def resolve(self, source_ref: str) -> Path:
        prefix = "local:"
        if not source_ref.startswith(prefix):
            raise SourceStoreError(f"unsupported source_ref: {source_ref}")
        revision = source_ref[len(prefix):]
        if not revision or "/" in revision or "\\" in revision:
            raise SourceStoreError("invalid local source_ref")
        path = (self.root / revision).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise SourceStoreError("source_ref escapes source root") from exc
        if not path.is_dir():
            raise SourceStoreError(f"source snapshot not found: {source_ref}")
        return path
