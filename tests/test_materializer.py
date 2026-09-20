import gzip
import io
import tarfile
import pytest

from runner_engine.worker.materialize import ReleaseMaterializer


def _archive(name: str, content=b"x"):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return gzip.compress(raw.getvalue(), mtime=0)


def test_rejects_parent_escape(tmp_path):
    import hashlib
    data = _archive("../escape")
    materializer = ReleaseMaterializer(tmp_path)
    with pytest.raises(ValueError):
        materializer.install("r", hashlib.sha256(data).hexdigest(), data)
