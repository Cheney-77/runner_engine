from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .materialize import ReleaseMaterializer


MAX_BODY = 16 * 1024 * 1024
MAX_USER_STDOUT = 1024 * 1024
MAX_USER_STDERR = 1024 * 1024
DEFAULT_MAX_OUTPUT = 8 * 1024 * 1024

PROTOCOL_MAGIC = b"MPR1"
PROTOCOL_VERSION = 1
PROTOCOL_OVERHEAD_BYTES = 2 * 1024 * 1024



def _linux_prctl(option: int, arg2: int) -> None:
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(option, arg2, 0, 0, 0)
    except Exception:
        pass


def _mark_agent_nondumpable() -> None:
    _linux_prctl(4, 0)  # PR_SET_DUMPABLE


def _kill_process_group(proc: subprocess.Popen) -> None:
    # Even if the leader has exited, descendants may still remain in its PGID.
    if os.name != "posix":
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        return

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def _kill_user_processes(uid: int) -> None:
    # Safe because one sandbox intentionally runs at most one user invocation.
    if not sys.platform.startswith("linux") or os.geteuid() != 0 or uid == 0:
        return

    for _ in range(3):
        found = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue

            pid = int(entry.name)
            if pid == os.getpid():
                continue

            try:
                text = (entry / "status").read_text(encoding="utf-8", errors="ignore")
                uid_line = next(
                    (line for line in text.splitlines() if line.startswith("Uid:")),
                    "",
                )
                real_uid = int(uid_line.split()[1]) if uid_line else -1
                if real_uid == uid:
                    os.kill(pid, signal.SIGKILL)
                    found = True
            except (
                FileNotFoundError,
                ProcessLookupError,
                PermissionError,
                ValueError,
                StopIteration,
            ):
                continue

        if not found:
            break
        time.sleep(0.02)


def _bounded_reader(
    stream,
    target: bytearray,
    cap: int,
    overflow: threading.Event,
    proc: subprocess.Popen,
) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return

            if len(target) + len(chunk) > cap:
                remaining = max(0, cap - len(target))
                if remaining:
                    target.extend(chunk[:remaining])
                overflow.set()
                _kill_process_group(proc)
                return

            target.extend(chunk)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _bounded_fd_reader(
    fd: int,
    target: bytearray,
    cap: int,
    overflow: threading.Event,
    proc: subprocess.Popen,
) -> None:
    try:
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                return

            if len(target) + len(chunk) > cap:
                remaining = max(0, cap - len(target))
                if remaining:
                    target.extend(chunk[:remaining])
                overflow.set()
                _kill_process_group(proc)
                return

            target.extend(chunk)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise BrokenPipeError("could not write child request")
        view = view[written:]


def _encode_frame(payload: bytes) -> bytes:
    return PROTOCOL_MAGIC + len(payload).to_bytes(4, "big", signed=False) + payload


def _decode_frame(raw: bytes, *, max_bytes: int) -> bytes:
    if len(raw) < 8:
        raise ValueError("child protocol frame is truncated")
    if raw[:4] != PROTOCOL_MAGIC:
        raise ValueError("invalid child protocol magic")

    size = int.from_bytes(raw[4:8], "big", signed=False)
    if size > max_bytes:
        raise ValueError("child protocol frame exceeds configured limit")
    if len(raw) != 8 + size:
        raise ValueError(
            f"child protocol length mismatch: declared={size}, actual={len(raw) - 8}"
        )
    return raw[8:]


def _tail(value: bytearray, limit: int = 4096) -> str:
    return bytes(value[-limit:]).decode("utf-8", "replace")


def _failure(
    code: str,
    message: str,
    *,
    duration_ms: int,
    retryable: bool = False,
) -> dict:
    return {
        "status": "FAILED",
        "relationship": "failure",
        "content_b64": "",
        "attributes": {},
        "retryable": retryable,
        "error_code": code,
        "error_message": message,
        "duration_ms": duration_ms,
    }


def _validate_child_result(
    raw: bytes,
    *,
    invocation_id: str,
    max_frame_bytes: int,
    max_output_bytes: int,
    max_attributes: int,
    max_attribute_bytes: int,
) -> dict:
    payload = _decode_frame(raw, max_bytes=max_frame_bytes)
    envelope = json.loads(payload)

    if not isinstance(envelope, dict):
        raise ValueError("child protocol response must be an object")
    if envelope.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("unsupported child protocol version")
    if envelope.get("type") != "result":
        raise ValueError("child protocol response type must be result")
    if envelope.get("invocation_id") != invocation_id:
        raise ValueError("child protocol invocation id mismatch")

    result = envelope.get("result")
    if not isinstance(result, dict):
        raise ValueError("child result must be an object")

    status = result.get("status")
    if status not in {"SUCCEEDED", "FAILED"}:
        raise ValueError("child result has invalid status")

    relationship = result.get("relationship")
    if not isinstance(relationship, str) or not relationship:
        raise ValueError("child result has invalid relationship")

    content_b64 = result.get("content_b64", "")
    if not isinstance(content_b64, str):
        raise ValueError("child result content_b64 must be a string")

    content = base64.b64decode(content_b64, validate=True)
    if len(content) > max_output_bytes:
        raise ValueError("child result content exceeds configured limit")

    attributes = result.get("attributes", {})
    if not isinstance(attributes, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in attributes.items()
    ):
        raise ValueError("child result attributes must be dict[str, str]")

    if len(attributes) > max_attributes:
        raise ValueError("child result has too many attributes")

    attribute_bytes = sum(
        len(key.encode("utf-8")) + len(value.encode("utf-8"))
        for key, value in attributes.items()
    )
    if attribute_bytes > max_attribute_bytes:
        raise ValueError("child result attributes exceed configured limit")

    retryable = result.get("retryable", False)
    if not isinstance(retryable, bool):
        raise ValueError("child result retryable must be a boolean")

    error_code = result.get("error_code", "")
    error_message = result.get("error_message", "")
    if not isinstance(error_code, str) or not isinstance(error_message, str):
        raise ValueError("child result error fields must be strings")

    return {
        "status": status,
        "relationship": relationship,
        "content_b64": content_b64,
        "attributes": attributes,
        "retryable": retryable,
        "error_code": error_code,
        "error_message": error_message,
    }


@dataclass
class _ActiveChild:
    invocation_id: str
    proc: subprocess.Popen
    cancel_requested: threading.Event
    scratch_dir: Path



class AgentState:
    # Trusted Agent: never imports user modules and never runs two user children at once.
    def __init__(
        self,
        *,
        token: str,
        releases_root: str | Path,
        dependency_root: str | Path | None = "/opt/python-deps",
        invocations_root: str | Path | None = None,
        child_uid: int = 10001,
        child_gid: int = 10001,
        child_nproc: int = 64,
        child_nofile: int = 256,
        child_address_space_bytes: int = 0,
    ):
        self.token = token
        self.materializer = ReleaseMaterializer(releases_root)
        self.releases_root = Path(releases_root).resolve()
        self.dependency_root = (
            Path(dependency_root).resolve() if dependency_root else None
        )

        if invocations_root is None:
            invocations_root = self.releases_root.parent / "invocations"

        self.invocations_root = Path(invocations_root).resolve()
        self.invocations_root.mkdir(parents=True, exist_ok=True)
        self.invocations_root.chmod(0o711)

        self.child_uid = child_uid
        self.child_gid = child_gid
        self.child_nproc = child_nproc
        self.child_nofile = child_nofile
        self.child_address_space_bytes = child_address_space_bytes


        self._releases: dict[str, tuple[Path, dict]] = {}
        self._lock = threading.RLock()
        self._run_gate = threading.Lock()
        self._active_lock = threading.RLock()
        self._active_child: _ActiveChild | None = None



    def bootstrap(self, payload: dict) -> dict:
        release_id = payload["release_id"]
        artifact = base64.b64decode(payload["artifact_b64"], validate=True)
        path = self.materializer.install(
            release_id,
            payload["artifact_sha256"],
            artifact,
        )
        manifest = json.loads((path / "operator.json").read_text(encoding="utf-8"))
        if manifest.get("id") != release_id:
            raise ValueError("release id in operator.json does not match bootstrap request")

        with self._lock:
            self._releases[release_id] = (path, manifest)

        return {"ok": True}

    def health(self) -> dict:
        with self._active_lock:
            busy = self._active_child is not None
        return {
            "ok": True,
            "busy": busy,
            "path_compatibility": "invocation-workspace-v2",
        }

    def _chown_for_child(self, path: Path) -> None:
        if os.geteuid() == 0:
            os.chown(path, self.child_uid, self.child_gid)

    def _make_tree_writable(self, root: Path) -> None:
        for current_root, dirs, files in os.walk(root):
            current = Path(current_root)
            self._chown_for_child(current)
            current.chmod((current.stat().st_mode & 0o777) | 0o700)

            for name in dirs:
                path = current / name
                self._chown_for_child(path)
                path.chmod((path.stat().st_mode & 0o777) | 0o700)

            for name in files:
                path = current / name
                self._chown_for_child(path)
                path.chmod((path.stat().st_mode & 0o777) | 0o600)

    def _prepare_scratch(self, invocation_id: str) -> Path:
        digest = hashlib.sha256(
            invocation_id.encode("utf-8", "replace")
        ).hexdigest()[:16]
        path = Path(
            tempfile.mkdtemp(prefix=f"{digest}-", dir=self.invocations_root)
        )

        for child_name in ("home", "tmp", "cache", "absolute"):
            child = path / child_name
            child.mkdir(mode=0o700)
            self._chown_for_child(child)

        self._chown_for_child(path)
        path.chmod(0o700)
        return path

    def _prepare_runtime_workspace(self, release_root: Path, scratch_dir: Path) -> Path:
        source = (release_root / "runtime").resolve()
        if not source.is_dir():
            raise RuntimeError(f"release runtime directory does not exist: {source}")

        workspace = scratch_dir / "workspace"
        shutil.copytree(source, workspace, symlinks=False)
        self._make_tree_writable(workspace)
        return workspace



    def _register_active(
        self,
        invocation_id: str,
        proc: subprocess.Popen,
        cancel_requested: threading.Event,
        scratch_dir: Path,
    ) -> None:
        with self._active_lock:
            if self._active_child is not None:
                raise RuntimeError("agent already has an active child")

            self._active_child = _ActiveChild(
                invocation_id=invocation_id,
                proc=proc,
                cancel_requested=cancel_requested,
                scratch_dir=scratch_dir,
            )

    def _clear_active(self, proc: subprocess.Popen) -> None:
        with self._active_lock:
            if self._active_child is not None and self._active_child.proc is proc:
                self._active_child = None

    def cancel(self, payload: dict) -> dict:
        invocation_id = str(payload.get("invocation_id") or "")
        if not invocation_id:
            raise ValueError("invocation_id is required")

        with self._active_lock:
            active = self._active_child
            if active is None or active.invocation_id != invocation_id:
                return {"cancelled": False}

            active.cancel_requested.set()
            proc = active.proc

        _kill_process_group(proc)
        return {"cancelled": True}

    def run(self, payload: dict) -> dict:
        invocation_id = str(
            payload.get("invocation_id") or f"legacy-{secrets.token_hex(8)}"
        )

        if not self._run_gate.acquire(blocking=False):
            return _failure(
                "AGENT_BUSY",
                "sandbox already has an active invocation",
                duration_ms=0,
                retryable=True,
            )

        scratch_dir = None
        proc = None
        request_read = request_write = -1
        result_read = result_write = -1

        try:
            release_id = payload["release_id"]
            with self._lock:
                pair = self._releases.get(release_id)

            if pair is None:
                raise ValueError("release is not bootstrapped")

            release_root, manifest = pair
            timeout_ms = max(1, int(payload.get("timeout_ms", 30_000)))
            max_output = max(
                1,
                int(payload.get("max_output_bytes", DEFAULT_MAX_OUTPUT)),
            )
            max_attributes = max(0, int(payload.get("max_attributes", 128)))
            max_attribute_bytes = max(
                0,
                int(payload.get("max_attribute_bytes", 65_536)),
            )
            max_protocol = (
                max_output * 2 + max_attribute_bytes + PROTOCOL_OVERHEAD_BYTES
            )

            scratch_dir = self._prepare_scratch(invocation_id)
            runtime_workspace = self._prepare_runtime_workspace(
                release_root,
                scratch_dir,
            )

            request = {
                "release_root": str(release_root),
                "dependency_root": (
                    str(self.dependency_root) if self.dependency_root else None
                ),
                "entrypoint": manifest["entrypoint"],
                "content_b64": payload.get("content_b64", ""),
                "attributes": payload.get("attributes", {}),
                "parameters": payload.get("parameters", {}),
            }
            envelope = {
                "protocol_version": PROTOCOL_VERSION,
                "type": "invoke",
                "invocation_id": invocation_id,
                "request": request,
            }
            raw = json.dumps(
                envelope,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            frame = _encode_frame(raw)

            if len(frame) > MAX_BODY:
                raise ValueError("child request exceeds configured limit")
            if os.name != "posix":
                raise RuntimeError("hardened child FD protocol currently requires POSIX")

            home = scratch_dir / "home"
            tmp_dir = scratch_dir / "tmp"
            cache = scratch_dir / "cache"

            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "HOME": str(home),
                "TMPDIR": str(tmp_dir),
                "XDG_CACHE_HOME": str(cache),
                "RUNNER_RUNTIME_ROOT": str(runtime_workspace),
                "RUNNER_INVOCATION_ROOT": str(scratch_dir),
                "RUNNER_MAX_OUTPUT_BYTES": str(max_output),
                "RUNNER_MAX_ATTRIBUTES": str(max_attributes),
                "RUNNER_MAX_ATTRIBUTE_BYTES": str(max_attribute_bytes),
                "RUNNER_CHILD_UID": str(self.child_uid),
                "RUNNER_CHILD_GID": str(self.child_gid),
                "RUNNER_CHILD_NPROC": str(self.child_nproc),
                "RUNNER_CHILD_NOFILE": str(self.child_nofile),
                "RUNNER_CHILD_CPU_SECONDS": str(
                    max(1, math.ceil(timeout_ms / 1000.0) + 1)
                ),
                "RUNNER_CHILD_ADDRESS_SPACE_BYTES": str(
                    self.child_address_space_bytes
                ),
            }

            request_read, request_write = os.pipe()
            result_read, result_write = os.pipe()

            child_script = str(Path(__file__).with_name("child.py"))
            started = time.monotonic()

            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    child_script,
                    "--request-fd",
                    str(request_read),
                    "--result-fd",
                    str(result_write),
                    "--invocation-id",
                    invocation_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                # Start the child in the disposable project workspace. This makes
                # normal project-relative paths correct even before the generated
                # adapter applies its pythonRoot-specific chdir.
                cwd=str(runtime_workspace),
                start_new_session=True,
                pass_fds=(request_read, result_write),
                close_fds=True,
            )

            os.close(request_read)
            request_read = -1
            os.close(result_write)
            result_write = -1

            assert proc.stdout and proc.stderr

            cancel_requested = threading.Event()
            self._register_active(
                invocation_id,
                proc,
                cancel_requested,
                scratch_dir,
            )

            stdout = bytearray()
            stderr = bytearray()
            protocol = bytearray()

            log_overflow = threading.Event()
            protocol_overflow = threading.Event()

            out_thread = threading.Thread(
                target=_bounded_reader,
                args=(
                    proc.stdout,
                    stdout,
                    MAX_USER_STDOUT,
                    log_overflow,
                    proc,
                ),
                daemon=True,
            )
            err_thread = threading.Thread(
                target=_bounded_reader,
                args=(
                    proc.stderr,
                    stderr,
                    MAX_USER_STDERR,
                    log_overflow,
                    proc,
                ),
                daemon=True,
            )
            result_thread = threading.Thread(
                target=_bounded_fd_reader,
                args=(
                    result_read,
                    protocol,
                    max_protocol + 8,
                    protocol_overflow,
                    proc,
                ),
                daemon=True,
            )
            result_read = -1

            out_thread.start()
            err_thread.start()
            result_thread.start()

            timed_out = False
            try:
                _write_all(request_write, frame)
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    os.close(request_write)
                except OSError:
                    pass
                request_write = -1

            try:
                proc.wait(timeout=timeout_ms / 1000.0)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_group(proc)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            _kill_process_group(proc)
            _kill_user_processes(self.child_uid)

            out_thread.join(timeout=2)
            err_thread.join(timeout=2)
            result_thread.join(timeout=2)

            duration = int((time.monotonic() - started) * 1000)

            if cancel_requested.is_set():
                return _failure(
                    "CANCELLED",
                    "operator invocation was cancelled",
                    duration_ms=duration,
                )

            if timed_out:
                return _failure(
                    "TIMEOUT",
                    f"operator exceeded {timeout_ms} ms",
                    duration_ms=duration,
                )

            if log_overflow.is_set():
                return _failure(
                    "LOG_LIMIT",
                    "operator stdout/stderr exceeded configured log limit",
                    duration_ms=duration,
                )

            if protocol_overflow.is_set():
                return _failure(
                    "CHILD_PROTOCOL_ERROR",
                    "child result protocol exceeded configured limit",
                    duration_ms=duration,
                )

            if not protocol:
                returncode = proc.returncode

                if returncode is not None and returncode < 0:
                    signum = -returncode
                    try:
                        signal_name = signal.Signals(signum).name
                    except Exception:
                        signal_name = str(signum)

                    code = (
                        "CPU_LIMIT"
                        if signum == getattr(signal, "SIGXCPU", -1)
                        else "USER_PROCESS_SIGNALED"
                    )
                    return _failure(
                        code,
                        (
                            f"operator child terminated by {signal_name}; "
                            f"stderr_tail={_tail(stderr)!r}"
                        ),
                        duration_ms=duration,
                    )

                return _failure(
                    "USER_PROCESS_EXITED",
                    (
                        "operator child exited without a protocol result "
                        f"(exit_code={returncode}); stderr_tail={_tail(stderr)!r}"
                    ),
                    duration_ms=duration,
                )

            try:
                response = _validate_child_result(
                    bytes(protocol),
                    invocation_id=invocation_id,
                    max_frame_bytes=max_protocol,
                    max_output_bytes=max_output,
                    max_attributes=max_attributes,
                    max_attribute_bytes=max_attribute_bytes,
                )
            except Exception as exc:
                return _failure(
                    "CHILD_PROTOCOL_ERROR",
                    (
                        f"invalid child protocol response: "
                        f"{type(exc).__name__}: {exc}; "
                        f"stderr_tail={_tail(stderr)!r}"
                    ),
                    duration_ms=duration,
                )

            response["duration_ms"] = duration
            return response

        finally:
            if proc is not None:
                self._clear_active(proc)
                _kill_process_group(proc)

            _kill_user_processes(self.child_uid)

            for fd in (request_read, request_write, result_read, result_write):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

            if scratch_dir is not None:
                shutil.rmtree(scratch_dir, ignore_errors=True)

            self._run_gate.release()


def _handler_for(state: AgentState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ManagedPythonAgent/3.3"

        def log_message(self, format, *args):
            return

        def _authorized(self) -> bool:
            return hmac.compare_digest(
                self.headers.get("X-Runner-Agent-Token", ""),
                state.token,
            )

        def _json(self, status: int, value: dict) -> None:
            body = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _payload(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc

            if length < 0 or length > MAX_BODY:
                raise ValueError("request body too large")

            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("request body must be a JSON object")
            return value

        def do_GET(self):
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return

            if self.path != "/health":
                self._json(404, {"error": "not found"})
                return

            self._json(200, state.health())

        def do_POST(self):
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return

            try:
                payload = self._payload()

                if self.path == "/bootstrap":
                    self._json(200, state.bootstrap(payload))
                elif self.path == "/run":
                    self._json(200, state.run(payload))
                elif self.path == "/cancel":
                    self._json(200, state.cancel(payload))
                else:
                    self._json(404, {"error": "not found"})
            except Exception as exc:
                self._json(
                    400,
                    {
                        "error": type(exc).__name__,
                        "message": str(exc),
                    },
                )

    return Handler


class _AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--listen",
        default=os.environ.get("RUNNER_AGENT_LISTEN", "0.0.0.0"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("RUNNER_AGENT_PORT", "9080")),
    )
    parser.add_argument(
        "--releases",
        default=os.environ.get("RUNNER_RELEASES_ROOT", "/opt/runner/releases"),
    )
    args = parser.parse_args()

    token = os.environ.get("RUNNER_AGENT_TOKEN")
    if not token:
        raise SystemExit("RUNNER_AGENT_TOKEN is required")


    _mark_agent_nondumpable()

    state = AgentState(
        token=token,
        releases_root=args.releases,
        dependency_root=os.environ.get(
            "RUNNER_DEPENDENCY_ROOT",
            "/opt/python-deps",
        ),
        invocations_root=os.environ.get(
            "RUNNER_INVOCATIONS_ROOT",
            "/opt/runner/invocations",
        ),
        child_uid=int(os.environ.get("RUNNER_CHILD_UID", "10001")),
        child_gid=int(os.environ.get("RUNNER_CHILD_GID", "10001")),
        child_nproc=int(os.environ.get("RUNNER_CHILD_NPROC", "64")),
        child_nofile=int(os.environ.get("RUNNER_CHILD_NOFILE", "256")),
        child_address_space_bytes=int(
            os.environ.get("RUNNER_CHILD_ADDRESS_SPACE_BYTES", "0")
        ),
    )

    print(
        f"[managed-python-agent] listening on {args.listen}:{args.port}",
        flush=True,
    )

    _AgentHTTPServer(
        (args.listen, args.port),
        _handler_for(state),
    ).serve_forever()


if __name__ == "__main__":
    main()
