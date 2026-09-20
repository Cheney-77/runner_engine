import base64
import hashlib
import io
import os
import json
import tarfile
import gzip

from runner_engine.worker.agent import AgentState


def _release(tmp_path, source: str):
    release_id = hashlib.sha256(source.encode()).hexdigest()
    manifest = json.dumps(
        {"abi": 1, "id": release_id, "entrypoint": "main:process"},
        sort_keys=True, separators=(",", ":")
    ).encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in [("main.py", source.encode()), ("operator.json", manifest)]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o444
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", filename="", mtime=0) as gz:
        gz.write(raw.getvalue())
    artifact = out.getvalue()
    state = AgentState(
        token="secret",
        releases_root=tmp_path / "releases",
        child_uid=os.getuid() if os.name == "posix" else 10001,
        child_gid=os.getgid() if os.name == "posix" else 10001,
        child_nproc=0,
    )
    state.bootstrap({
        "release_id": release_id,
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "artifact_b64": base64.b64encode(artifact).decode(),
    })
    return state, release_id


def test_os_exit_only_kills_child(tmp_path):
    state, release_id = _release(
        tmp_path,
        "import os\ndef process(content, attributes, parameters):\n    os._exit(7)\n",
    )
    first = state.run({
        "release_id": release_id,
        "content_b64": "",
        "timeout_ms": 2000,
    })
    assert first["error_code"] == "USER_PROCESS_EXITED"

    # Trusted agent object/process is still alive and can run another child invocation.
    second = state.run({
        "release_id": release_id,
        "content_b64": "",
        "timeout_ms": 2000,
    })
    assert second["error_code"] == "USER_PROCESS_EXITED"


def test_interpreter_globals_do_not_leak_between_invocations(tmp_path):
    state, release_id = _release(
        tmp_path,
        "counter = 0\n"
        "def process(content, attributes, parameters):\n"
        "    global counter\n"
        "    counter += 1\n"
        "    return str(counter)\n",
    )
    outputs = []
    for _ in range(2):
        response = state.run({
            "release_id": release_id,
            "content_b64": "",
            "timeout_ms": 2000,
        })
        outputs.append(base64.b64decode(response["content_b64"]))
    assert outputs == [b"1", b"1"]


def test_timeout_kills_user_process(tmp_path):
    state, release_id = _release(
        tmp_path,
        "def process(content, attributes, parameters):\n"
        "    while True:\n"
        "        pass\n",
    )
    response = state.run({
        "release_id": release_id,
        "content_b64": "",
        "timeout_ms": 100,
    })
    assert response["error_code"] == "TIMEOUT"
