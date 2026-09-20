from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from operator_authoring.compiler import ExecutionPlan
from operator_authoring.model import VirtualOperatorContract
from operator_authoring.snapshot import read_requirements

from .base import backend_variant_key
from .models import NativeProperty, NifiNativeBackendContract

COMPILER_VERSION = "nifi-native-contract-v1"


def _identifier(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    if not value:
        value = "GeneratedOperator"
    if value[0].isdigit():
        value = "_" + value
    return value


def _class_name(name: str, suffix: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", name) if part]
    base = "".join(part[:1].upper() + part[1:] for part in parts) or "GeneratedOperator"
    if base[0].isdigit():
        base = "Operator" + base
    return f"{base}_{suffix}"


def compile_nifi_native_contract(
    *,
    contract_id: str,
    contract_version: int,
    parent: VirtualOperatorContract,
    plan: ExecutionPlan,
    source_path: Path,
    options: dict[str, Any],
) -> NifiNativeBackendContract:
    requirements = read_requirements(source_path)
    class_name = str(options.get("className") or _class_name(parent.metadata.name, plan.contract_sha256[:8]))
    package_name = str(options.get("packageName") or _identifier(f"dsc_{parent.metadata.name}_{plan.contract_sha256[:8]}"))
    normalized_options = {"className": class_name, "packageName": package_name}

    variant_key = backend_variant_key(
        contract_sha256=plan.contract_sha256,
        backend="nifi_native",
        compiler_version=COMPILER_VERSION,
        options=normalized_options,
    )

    return NifiNativeBackendContract(
        **parent.model_dump(mode="python"),
        parent_contract_id=contract_id,
        parent_contract_version=contract_version,
        parent_contract_sha256=plan.contract_sha256,
        compiler_version=COMPILER_VERSION,
        variant_key=variant_key,
        backend_options=normalized_options,
        class_name=class_name,
        package_name=package_name,
        properties=[
            NativeProperty(
                key=item.key,
                display_name=item.display_name,
                description=item.description,
                required=item.required,
                has_default=item.has_default,
                default=item.default,
            )
            for item in plan.parameters
        ],
        requirements=requirements,
    )


_NATIVE_TEMPLATE = r'''from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor

_MISSING = object()
_PACKAGE_ROOT = Path(__file__).resolve().parent
_PLAN = json.loads(__PLAN_JSON__)
_PROPERTIES = json.loads(__PROPERTIES_JSON__)


def _runtime_python_root():
    python_path = _PLAN.get("python_path", ".")
    root = (_PACKAGE_ROOT / "runtime" / python_path).resolve()
    runtime_root = (_PACKAGE_ROOT / "runtime").resolve()
    try:
        root.relative_to(runtime_root)
    except ValueError as exc:
        raise RuntimeError("compiled python_path escapes runtime root") from exc
    return root


def _decode(raw, codec):
    if isinstance(raw, bytearray):
        raw = bytes(raw)
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


def _resolve_binding(binding, content, metadata, parameters):
    source = binding["source"]
    codec = binding.get("codec", "text")

    if source == "input.payload":
        return _decode(content, codec)

    if source == "input.metadata":
        key = binding["metadata_key"]
        if key not in metadata or metadata[key] is None:
            if binding.get("required", False):
                raise ValueError(f"required input metadata is missing: {key}")
            return _MISSING
        return _decode(metadata[key], codec)

    if source == "operator.parameter":
        key = binding["parameter_key"]
        if key in parameters and parameters[key] is not None:
            return _decode(parameters[key], codec)
        if binding.get("has_default", False):
            return binding.get("default")
        if binding.get("required", False):
            raise ValueError(f"required operator parameter is missing: {key}")
        return _MISSING

    if source == "constant":
        return binding.get("constant")

    raise ValueError(f"unsupported binding source: {source}")


def _build_kwargs(bindings, content, metadata, parameters):
    kwargs = {}
    for name, binding in bindings.items():
        value = _resolve_binding(binding, content, metadata, parameters)
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
        return str(value)
    if codec == "json":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    raise ValueError(f"unsupported output codec: {codec}")


def _metadata_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class __CLASS_NAME__(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "1.0.0"
        description = __DESCRIPTION__

    def __init__(self, **kwargs):
        self.descriptors = []
        for item in _PROPERTIES:
            default_value = None
            if item.get("has_default", False):
                value = item.get("default")
                default_value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            descriptor = PropertyDescriptor(
                name=item["key"],
                description=item.get("description") or item.get("display_name") or item["key"],
                default_value=default_value,
                required=item.get("required", False),
            )
            self.descriptors.append(descriptor)

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowfile):
        content = flowfile.getContentsAsBytes()
        metadata = flowfile.getAttributes()
        parameters = {}
        for item in _PROPERTIES:
            value = context.getProperty(item["key"]).evaluateAttributeExpressions(flowfile).getValue()
            if value is not None:
                parameters[item["key"]] = value

        runtime_python_root = _runtime_python_root()
        original_path = list(sys.path)
        sys.path.insert(0, str(runtime_python_root))

        try:
            target_spec = _PLAN["target"]
            constructor_kwargs = _build_kwargs(
                _PLAN.get("constructor_arguments", {}), content, metadata, parameters
            )
            invoke_kwargs = _build_kwargs(_PLAN.get("arguments", {}), content, metadata, parameters)

            if target_spec["kind"] == "instance-method":
                cls = _resolve_qualname(target_spec["class_module"], target_spec["class_qualname"])
                instance = cls(**constructor_kwargs)
                target = getattr(instance, target_spec["method_name"])
            else:
                target = _resolve_qualname(target_spec["module"], target_spec["qualname"])

            result = target(**invoke_kwargs)
            output_spec = _PLAN["output"]
            payload_value = _output_value(result, output_spec["payload"])

            if payload_value is None and output_spec.get("none_policy") == "keep-original":
                output_content = None
            elif payload_value is None:
                output_content = b""
            else:
                output_content = _encode_payload(payload_value, output_spec["payload"]["codec"])

            output_metadata = {}
            for name, spec in output_spec.get("metadata", {}).items():
                output_metadata[name] = _metadata_text(_output_value(result, spec))

            return FlowFileTransformResult(
                relationship="success",
                contents=output_content,
                attributes=output_metadata or None,
            )
        finally:
            sys.path[:] = original_path
'''


def _native_source(contract: NifiNativeBackendContract, plan: ExecutionPlan) -> str:
    plan_json = json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    properties_json = json.dumps(
        [item.model_dump(mode="json") for item in contract.properties],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return (
        _NATIVE_TEMPLATE
        .replace("__PLAN_JSON__", repr(plan_json))
        .replace("__PROPERTIES_JSON__", repr(properties_json))
        .replace("__CLASS_NAME__", contract.class_name)
        .replace("__DESCRIPTION__", repr(contract.metadata.description or contract.metadata.display_name))
    )


def write_native_package(
    output_root: Path,
    *,
    contract: NifiNativeBackendContract,
    plan: ExecutionPlan,
    source_path: Path,
) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    final_zip = output_root / f"{contract.package_name}.zip"

    with tempfile.TemporaryDirectory(prefix="nifi-native-package-") as temp:
        package_root = Path(temp) / contract.package_name
        package_root.mkdir(parents=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        (package_root / f"{contract.class_name}.py").write_text(
            _native_source(contract, plan),
            encoding="utf-8",
        )

        shutil.copy2(source_path / "requirements.txt", package_root / "requirements.txt")
        shutil.copytree(source_path, package_root / "runtime")

        if final_zip.exists():
            final_zip.unlink()
        with zipfile.ZipFile(final_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(package_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(package_root.parent).as_posix())

    return final_zip
