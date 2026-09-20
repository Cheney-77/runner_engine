from __future__ import annotations

import json

from .compiler import ExecutionPlan


_TEMPLATE = r'''from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

_MISSING = object()
_RELEASE_ROOT = Path(__file__).resolve().parent
_PLAN = json.loads(__PLAN_JSON__)


def _runtime_root():
    configured = os.environ.get("RUNNER_RUNTIME_ROOT")
    root = Path(configured).resolve() if configured else (_RELEASE_ROOT / "runtime").resolve()
    if not root.is_dir():
        raise RuntimeError(f"runtime root does not exist: {root}")
    return root


def _runtime_python_root():
    runtime_root = _runtime_root()
    python_path = _PLAN.get("python_path", ".")
    root = (runtime_root / python_path).resolve()

    try:
        root.relative_to(runtime_root)
    except ValueError as exc:
        raise RuntimeError("compiled python_path escapes runtime root") from exc

    if not root.is_dir():
        raise RuntimeError(f"compiled python root does not exist: {root}")
    return root


def _decode(raw, codec):
    if codec == "bytes":
        if isinstance(raw, bytes):
            return raw
        if isinstance(raw, str):
            return raw.encode("utf-8")
        raise TypeError("bytes codec expects bytes or str")
    if codec == "text":
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return str(raw)
    if codec == "json":
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            return json.loads(raw)
        return raw
    raise ValueError(f"unsupported codec: {codec}")


def _resolve_binding(binding, content, attributes, parameters):
    source = binding["source"]
    codec = binding.get("codec", "text")

    if source == "input.payload":
        return _decode(content, codec)

    if source == "input.metadata":
        key = binding["metadata_key"]
        if key not in attributes:
            if binding.get("required", False):
                raise ValueError(f"required input metadata is missing: {key}")
            return _MISSING
        return _decode(attributes[key], codec)

    if source == "operator.parameter":
        key = binding["parameter_key"]
        if key in parameters:
            return _decode(parameters[key], codec)
        if binding.get("has_default", False):
            return binding.get("default")
        if binding.get("required", False):
            raise ValueError(f"required operator parameter is missing: {key}")
        return _MISSING

    if source == "constant":
        return binding.get("constant")

    raise ValueError(f"unsupported binding source: {source}")


def _build_kwargs(bindings, content, attributes, parameters):
    kwargs = {}
    for name, binding in bindings.items():
        value = _resolve_binding(binding, content, attributes, parameters)
        if value is not _MISSING:
            kwargs[name] = value
    return kwargs


def _resolve_qualname(module_name, qualname):
    target = importlib.import_module(module_name)
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def _extract(value, path):
    current = value
    for part in path or []:
        if isinstance(part, int):
            current = current[part]
        elif isinstance(current, dict):
            current = current[part]
        else:
            current = getattr(current, part)
    return current


def _output_value(result, spec):
    source = spec.get("source", "return")
    if source == "return":
        return result
    if source == "return.path":
        return _extract(result, spec.get("path"))
    if source == "constant":
        return spec.get("value")
    raise ValueError(f"unsupported output source: {source}")


def _encode_payload(value, codec):
    if codec == "bytes":
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        raise TypeError("bytes output codec expects bytes, bytearray or str")
    if codec == "text":
        return str(value).encode("utf-8")
    if codec == "json":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    raise ValueError(f"unsupported output codec: {codec}")


def _metadata_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def process(content, attributes, parameters):
    runtime_python_root = _runtime_python_root()
    original_path = list(sys.path)
    original_cwd = Path.cwd()

    sys.path.insert(0, str(runtime_python_root))
    try:
        # User code sees pythonRoot as both its import root and working directory.
        # The canonical release remains immutable; Agent points RUNNER_RUNTIME_ROOT
        # at a disposable per-invocation writable copy.
        os.chdir(runtime_python_root)

        target_spec = _PLAN["target"]
        constructor_kwargs = _build_kwargs(
            _PLAN.get("constructor_arguments", {}), content, attributes, parameters
        )
        invoke_kwargs = _build_kwargs(
            _PLAN.get("arguments", {}), content, attributes, parameters
        )

        if target_spec["kind"] == "instance-method":
            cls = _resolve_qualname(
                target_spec["class_module"], target_spec["class_qualname"]
            )
            instance = cls(**constructor_kwargs)
            target = getattr(instance, target_spec["method_name"])
        else:
            target = _resolve_qualname(target_spec["module"], target_spec["qualname"])

        result = target(**invoke_kwargs)
        output_spec = _PLAN["output"]
        payload_value = _output_value(result, output_spec["payload"])

        if payload_value is None:
            output_content = (
                content if output_spec.get("none_policy") == "keep-original" else b""
            )
        else:
            output_content = _encode_payload(
                payload_value, output_spec["payload"]["codec"]
            )

        output_attributes = {}
        for name, spec in output_spec.get("metadata", {}).items():
            output_attributes[name] = _metadata_text(_output_value(result, spec))

        return {
            "content": output_content,
            "attributes": output_attributes,
            "relationship": "success",
        }
    finally:
        try:
            os.chdir(original_cwd)
        finally:
            sys.path[:] = original_path
'''


def generate_runner_adapter(plan: ExecutionPlan) -> str:
    plan_json = json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _TEMPLATE.replace("__PLAN_JSON__", repr(plan_json))
