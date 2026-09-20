from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import resource
import sys
import traceback
import importlib.util
from pathlib import Path


PROTOCOL_MAGIC = b"MPR1"
PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 20 * 1024 * 1024


class OutputLimitError(ValueError):
    pass


class InvalidUserResultError(ValueError):
    pass


def _linux_prctl(option: int, arg2: int) -> None:
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(option, arg2, 0, 0, 0)
    except Exception:
        pass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _set_limit(which: int, soft: int, hard: int | None = None) -> None:
    if soft < 0:
        return
    try:
        resource.setrlimit(which, (soft, soft if hard is None else hard))
    except Exception:
        pass


def _harden_process() -> None:
    # Trusted runner bootstrap: apply restrictions before importing user code.
    _linux_prctl(38, 1)  # PR_SET_NO_NEW_PRIVS
    _set_limit(resource.RLIMIT_CORE, 0)

    nofile = _int_env("RUNNER_CHILD_NOFILE", 256)
    if nofile > 0:
        _set_limit(resource.RLIMIT_NOFILE, nofile)

    nproc = _int_env("RUNNER_CHILD_NPROC", 64)
    if nproc > 0 and hasattr(resource, "RLIMIT_NPROC"):
        _set_limit(resource.RLIMIT_NPROC, nproc)

    cpu_seconds = _int_env("RUNNER_CHILD_CPU_SECONDS", 0)
    if cpu_seconds > 0 and hasattr(resource, "RLIMIT_CPU"):
        _set_limit(resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 1)

    # Disabled by default because mmap-heavy libraries may reserve large VA ranges.
    address_space_bytes = _int_env("RUNNER_CHILD_ADDRESS_SPACE_BYTES", 0)
    if address_space_bytes > 0 and hasattr(resource, "RLIMIT_AS"):
        _set_limit(resource.RLIMIT_AS, address_space_bytes)

    uid = _int_env("RUNNER_CHILD_UID", os.geteuid())
    gid = _int_env("RUNNER_CHILD_GID", os.getegid())

    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)


def _read_exact(fd: int, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise EOFError(f"protocol stream ended with {remaining} bytes remaining")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(fd: int, *, max_bytes: int) -> bytes:
    header = _read_exact(fd, 8)
    if header[:4] != PROTOCOL_MAGIC:
        raise ValueError("invalid child protocol magic")

    size = int.from_bytes(header[4:8], "big", signed=False)
    if size > max_bytes:
        raise ValueError(f"protocol frame exceeds limit: {size}")

    payload = _read_exact(fd, size)
    if os.read(fd, 1):
        raise ValueError("unexpected trailing bytes after protocol frame")
    return payload


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise BrokenPipeError("could not write child protocol frame")
        view = view[written:]


def _write_frame(fd: int, payload: bytes) -> None:
    if len(payload) > MAX_RESULT_BYTES:
        raise ValueError("child result protocol frame exceeds limit")
    header = PROTOCOL_MAGIC + len(payload).to_bytes(4, "big", signed=False)
    _write_all(fd, header + payload)


def _traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-8192:]


def _failed(code: str, message: str) -> dict:
    return {
        "status": "FAILED",
        "relationship": "failure",
        "content_b64": "",
        "attributes": {},
        "retryable": False,
        "error_code": code,
        "error_message": message,
    }


def _encode_content(value) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    raise InvalidUserResultError(
        "operator result content must be bytes, bytearray, str, or None"
    )


def _normalize_result(value, original_content: bytes) -> dict:
    if value is None:
        return {
            "status": "SUCCEEDED",
            "content": original_content,
            "attributes": {},
            "relationship": "success",
            "retryable": False,
            "error_code": "",
            "error_message": "",
        }

    if isinstance(value, (bytes, bytearray, str)):
        return {
            "status": "SUCCEEDED",
            "content": _encode_content(value),
            "attributes": {},
            "relationship": "success",
            "retryable": False,
            "error_code": "",
            "error_message": "",
        }

    if not isinstance(value, dict):
        raise InvalidUserResultError("operator must return bytes, str, dict or None")

    attributes = value.get("attributes", {})
    if not isinstance(attributes, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in attributes.items()
    ):
        raise InvalidUserResultError("result attributes must be dict[str, str]")

    retryable = value.get("retryable", False)
    if not isinstance(retryable, bool):
        raise InvalidUserResultError("retryable must be a boolean")

    status = value.get("status")
    if status is None:
        status = "FAILED" if retryable else "SUCCEEDED"
    if status not in {"SUCCEEDED", "FAILED"}:
        raise InvalidUserResultError("status must be SUCCEEDED or FAILED")
    if status == "SUCCEEDED" and retryable:
        raise InvalidUserResultError("a successful result cannot be retryable")

    default_relationship = (
        "retry" if retryable else ("failure" if status == "FAILED" else "success")
    )
    relationship = value.get("relationship", default_relationship)
    if not isinstance(relationship, str) or not relationship:
        raise InvalidUserResultError("relationship must be a non-empty string")

    error_code = value.get("error_code", "")
    error_message = value.get("error_message", "")
    if not isinstance(error_code, str) or not isinstance(error_message, str):
        raise InvalidUserResultError("error_code and error_message must be strings")

    if status == "FAILED" and not error_code:
        error_code = "OPERATOR_REPORTED_FAILURE"

    return {
        "status": status,
        "content": _encode_content(value.get("content", original_content)),
        "attributes": attributes,
        "relationship": relationship,
        "retryable": retryable,
        "error_code": error_code,
        "error_message": error_message,
    }


def _validate_output(content: bytes, attributes: dict[str, str]) -> None:
    max_output = _int_env("RUNNER_MAX_OUTPUT_BYTES", 8 * 1024 * 1024)
    max_attributes = _int_env("RUNNER_MAX_ATTRIBUTES", 128)
    max_attribute_bytes = _int_env("RUNNER_MAX_ATTRIBUTE_BYTES", 65_536)

    if len(content) > max_output:
        raise OutputLimitError("operator output exceeds byte limit")
    if len(attributes) > max_attributes:
        raise OutputLimitError("operator output has too many attributes")

    attribute_bytes = sum(
        len(key.encode("utf-8")) + len(value.encode("utf-8"))
        for key, value in attributes.items()
    )
    if attribute_bytes > max_attribute_bytes:
        raise OutputLimitError("operator output attributes exceed byte limit")




def _runtime_context() -> str:
    runtime_root = os.environ.get("RUNNER_RUNTIME_ROOT")
    try:
        cwd = os.getcwd()
    except Exception:
        cwd = "<unavailable>"
    return f"\n[runner-context] cwd={cwd!r}, runtime_root={runtime_root!r}"

def _install_path_virtualization():
    module_path = Path(__file__).with_name("path_virtualization.py")
    spec = importlib.util.spec_from_file_location(
        "_mpr_path_virtualization",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load path_virtualization.py")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.install_from_env()

def execute(request: dict) -> dict:
    try:
        release_root = Path(request["release_root"]).resolve()
        dependency_root = request.get("dependency_root")
        entrypoint = request["entrypoint"]
        module_name, function_name = entrypoint.split(":", 1)

        content = base64.b64decode(request.get("content_b64", ""), validate=True)
        attributes = dict(request.get("attributes", {}))
        parameters = dict(request.get("parameters", {}))
    except BaseException as exc:
        return _failed("CHILD_PROTOCOL_ERROR", _traceback(exc))

    original_sys_path = list(sys.path)
    import_paths = [str(release_root)]

    if dependency_root:
        dependency_path = Path(dependency_root).resolve()
        if dependency_path.is_dir():
            import_paths.append(str(dependency_path))

    sys.path[:] = import_paths + original_sys_path
    path_layer = None

    try:
        try:
            path_layer = _install_path_virtualization()
        except BaseException as exc:
            return _failed("FILESYSTEM_COMPAT_ERROR", _traceback(exc))

        try:
            module = __import__(module_name, fromlist=["*"])
        except BaseException as exc:
            return _failed("USER_IMPORT_ERROR", _traceback(exc) + _runtime_context())

        try:
            target = getattr(module, function_name)
        except BaseException as exc:
            return _failed("ENTRYPOINT_NOT_FOUND", _traceback(exc))

        try:
            value = target(
                content=content,
                attributes=attributes,
                parameters=parameters,
            )
        except BaseException as exc:
            return _failed("USER_EXCEPTION", _traceback(exc) + _runtime_context())

        try:
            normalized = _normalize_result(value, content)
            _validate_output(normalized["content"], normalized["attributes"])
        except OutputLimitError as exc:
            return _failed("OUTPUT_LIMIT", str(exc))
        except BaseException as exc:
            return _failed("INVALID_USER_RESULT", _traceback(exc))

        return {
            "status": normalized["status"],
            "relationship": normalized["relationship"],
            "content_b64": base64.b64encode(normalized["content"]).decode("ascii"),
            "attributes": normalized["attributes"],
            "retryable": normalized["retryable"],
            "error_code": normalized["error_code"],
            "error_message": normalized["error_message"],
        }
    finally:
        if path_layer is not None:
            try:
                path_layer.uninstall()
            except Exception:
                pass
        sys.path[:] = original_sys_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-fd", type=int, required=True)
    parser.add_argument("--result-fd", type=int, required=True)
    parser.add_argument("--invocation-id", required=True)
    args = parser.parse_args()

    request_fd = args.request_fd
    result_fd = args.result_fd
    invocation_id = args.invocation_id

    # User-created subprocesses must not inherit the result protocol descriptor.
    try:
        os.set_inheritable(result_fd, False)
    except Exception:
        pass

    try:
        _harden_process()

        raw = _read_frame(request_fd, max_bytes=MAX_REQUEST_BYTES)
        os.close(request_fd)
        request_fd = -1

        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            raise ValueError("child request envelope must be an object")
        if envelope.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError("unsupported child protocol version")
        if envelope.get("type") != "invoke":
            raise ValueError("child request type must be invoke")
        if envelope.get("invocation_id") != invocation_id:
            raise ValueError("child invocation id mismatch")

        request = envelope.get("request")
        if not isinstance(request, dict):
            raise ValueError("child request payload must be an object")

        result = execute(request)
    except BaseException as exc:
        result = _failed("CHILD_PROTOCOL_ERROR", _traceback(exc))
    finally:
        if request_fd >= 0:
            try:
                os.close(request_fd)
            except OSError:
                pass

    response = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "result",
        "invocation_id": invocation_id,
        "result": result,
    }

    try:
        payload = json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
        _write_frame(result_fd, payload)
    finally:
        try:
            os.close(result_fd)
        except OSError:
            pass


if __name__ == "__main__":
    main()
