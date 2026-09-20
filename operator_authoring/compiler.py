from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from .model import (
    ArgumentBinding, BindingSource, CallableInfo, CallableKind, OperatorParameterSpec,
    ParameterInfo, ProjectCatalog, VirtualOperatorContract,
)

COMPILER_VERSION = "virtual-direct-v1"
SUPPORTED_PARAMETER_KINDS = {"POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY"}


class ContractCompilationError(ValueError):
    pass


class CompiledParameter(BaseModel):
    key: str
    display_name: str
    description: str = ""
    required: bool = False
    has_default: bool = False
    default: Any | None = None


class ExecutionPlan(BaseModel):
    compiler_version: str = COMPILER_VERSION
    source_revision: str
    source_ref: str | None = None
    python_path: str
    target: dict[str, Any]
    constructor_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    output: dict[str, Any]
    parameters: list[CompiledParameter] = Field(default_factory=list)
    input_metadata: list[str] = Field(default_factory=list)
    output_metadata: list[str] = Field(default_factory=list)
    contract_sha256: str


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def contract_sha256(contract: VirtualOperatorContract) -> str:
    payload = contract.model_dump(mode="json")
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def callable_support_reasons(callable_info: CallableInfo, catalog: ProjectCatalog) -> list[str]:
    reasons: list[str] = []
    if callable_info.is_async:
        reasons.append("async callable is not supported by Direct Invoke v1")

    for parameter in callable_info.parameters:
        if parameter.kind not in SUPPORTED_PARAMETER_KINDS:
            reasons.append(f"parameter {parameter.name!r} uses unsupported kind {parameter.kind}")

    if callable_info.kind == CallableKind.INSTANCE_METHOD:
        if not callable_info.class_target:
            reasons.append("instance method has no class metadata")
        else:
            class_info = catalog.class_by_target(callable_info.class_target)
            if class_info is None:
                reasons.append("class metadata is missing")
            else:
                for parameter in class_info.constructor:
                    if parameter.kind not in SUPPORTED_PARAMETER_KINDS:
                        reasons.append(
                            f"constructor parameter {parameter.name!r} uses unsupported kind {parameter.kind}"
                        )
    return reasons


def _parameter_map(parameters: list[ParameterInfo]) -> dict[str, ParameterInfo]:
    return {item.name: item for item in parameters}


def _validate_bindings(
    parameters: list[ParameterInfo],
    bindings: dict[str, ArgumentBinding],
    *,
    label: str,
) -> None:
    known = _parameter_map(parameters)
    unknown = sorted(set(bindings) - set(known))
    if unknown:
        raise ContractCompilationError(f"{label} contains bindings for unknown parameters: {unknown}")

    missing = [item.name for item in parameters if item.required and item.name not in bindings]
    if missing:
        raise ContractCompilationError(f"{label} is missing required parameter bindings: {missing}")


def _compile_binding(
    parameter: ParameterInfo,
    binding: ArgumentBinding,
    *,
    parameters: dict[str, CompiledParameter],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": binding.source.value,
        "codec": binding.codec.value,
        "required": parameter.required,
        "metadata_key": binding.metadata_key,
        "parameter_key": None,
        "constant": binding.constant,
    }

    if binding.source == BindingSource.OPERATOR_PARAMETER:
        assert binding.parameter is not None
        spec: OperatorParameterSpec = binding.parameter
        compiled = CompiledParameter(
            key=spec.key,
            display_name=spec.display_name or spec.key,
            description=spec.description,
            required=spec.required or parameter.required,
            has_default=spec.has_default,
            default=spec.default,
        )
        existing = parameters.get(spec.key)
        if existing is not None and existing.model_dump() != compiled.model_dump():
            raise ContractCompilationError(f"conflicting definitions for operator parameter {spec.key!r}")
        parameters[spec.key] = compiled
        payload["parameter_key"] = spec.key
        payload["required"] = compiled.required
        payload["has_default"] = compiled.has_default
        payload["default"] = compiled.default

    return payload


def _compile_bindings(
    parameter_defs: list[ParameterInfo],
    bindings: dict[str, ArgumentBinding],
    *,
    parameters: dict[str, CompiledParameter],
) -> dict[str, dict[str, Any]]:
    by_name = _parameter_map(parameter_defs)
    return {
        name: _compile_binding(by_name[name], binding, parameters=parameters)
        for name, binding in bindings.items()
    }


def _target_payload(callable_info: CallableInfo) -> dict[str, Any]:
    payload = {
        "kind": callable_info.kind.value,
        "module": callable_info.module,
        "qualname": callable_info.qualname,
        "class_target": callable_info.class_target,
        "method_name": callable_info.method_name,
    }
    if callable_info.class_target:
        module, class_qualname = callable_info.class_target.split(":", 1)
        payload["class_module"] = module
        payload["class_qualname"] = class_qualname
    return payload


def compile_virtual_contract(contract: VirtualOperatorContract, catalog: ProjectCatalog) -> ExecutionPlan:
    if contract.source.source_revision != catalog.source_revision:
        raise ContractCompilationError(
            "source revision does not match the immutable source snapshot"
        )
    if contract.source.python_path != catalog.python_path:
        raise ContractCompilationError("contract python_path does not match the analyzed project")

    blocking = [item for item in catalog.warnings if item.severity == "error"]
    if blocking:
        messages = "; ".join(f"{item.file}:{item.line or 0}: {item.message}" for item in blocking)
        raise ContractCompilationError(f"source contains Python syntax errors: {messages}")

    callable_info = catalog.callable_by_id(contract.target.callable_id)
    if callable_info is None:
        raise ContractCompilationError(f"selected callable not found: {contract.target.callable_id}")

    reasons = callable_support_reasons(callable_info, catalog)
    if reasons:
        raise ContractCompilationError("; ".join(reasons))

    constructor_defs: list[ParameterInfo] = []
    if callable_info.kind == CallableKind.INSTANCE_METHOD:
        class_info = catalog.class_by_target(callable_info.class_target or "")
        if class_info is None:
            raise ContractCompilationError(f"selected class not found: {callable_info.class_target}")
        constructor_defs = class_info.constructor
        _validate_bindings(
            constructor_defs,
            contract.constructor_arguments,
            label="constructor arguments",
        )
    elif contract.constructor_arguments:
        raise ContractCompilationError("constructor arguments are only valid for instance methods")

    _validate_bindings(callable_info.parameters, contract.arguments, label="invoke arguments")

    parameters: dict[str, CompiledParameter] = {}
    constructor_arguments = _compile_bindings(
        constructor_defs,
        contract.constructor_arguments,
        parameters=parameters,
    )
    arguments = _compile_bindings(
        callable_info.parameters,
        contract.arguments,
        parameters=parameters,
    )

    all_bindings = [*constructor_arguments.values(), *arguments.values()]
    input_metadata = sorted({
        item["metadata_key"]
        for item in all_bindings
        if item["source"] == BindingSource.INPUT_METADATA.value and item["metadata_key"]
    })
    output_metadata = sorted(contract.output.metadata)

    return ExecutionPlan(
        source_revision=contract.source.source_revision,
        source_ref=contract.source.source_ref,
        python_path=contract.source.python_path,
        target=_target_payload(callable_info),
        constructor_arguments=constructor_arguments,
        arguments=arguments,
        output=contract.output.model_dump(mode="json"),
        parameters=sorted(parameters.values(), key=lambda item: item.key),
        input_metadata=input_metadata,
        output_metadata=output_metadata,
        contract_sha256=contract_sha256(contract),
    )
