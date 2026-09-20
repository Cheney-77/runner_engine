from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class CallableKind(StrEnum):
    FUNCTION = "function"
    INSTANCE_METHOD = "instance-method"
    STATIC_METHOD = "static-method"
    CLASS_METHOD = "class-method"


class BindingSource(StrEnum):
    INPUT_PAYLOAD = "input.payload"
    INPUT_METADATA = "input.metadata"
    OPERATOR_PARAMETER = "operator.parameter"
    CONSTANT = "constant"


class Codec(StrEnum):
    BYTES = "bytes"
    TEXT = "text"
    JSON = "json"


class NonePolicy(StrEnum):
    KEEP_ORIGINAL = "keep-original"
    EMPTY = "empty"


class OutputValueSource(StrEnum):
    RETURN = "return"
    RETURN_PATH = "return.path"
    CONSTANT = "constant"


class CatalogWarning(BaseModel):
    severity: Literal["warning", "error"]
    file: str
    line: int | None = None
    message: str


class ParameterInfo(BaseModel):
    name: str
    kind: str
    annotation: str | None = None
    required: bool = True
    has_default: bool = False
    default_value: Any | None = None
    default_repr: str | None = None


class CallableInfo(BaseModel):
    id: str
    module: str
    qualname: str
    kind: CallableKind
    class_target: str | None = None
    method_name: str | None = None
    file: str
    line: int
    is_async: bool = False
    decorators: list[str] = Field(default_factory=list)
    score: int = 0
    parameters: list[ParameterInfo] = Field(default_factory=list)
    return_annotation: str | None = None


class ClassInfo(BaseModel):
    id: str
    module: str
    qualname: str
    target: str
    file: str
    line: int
    constructor: list[ParameterInfo] = Field(default_factory=list)
    methods: list[CallableInfo] = Field(default_factory=list)


class ProjectCatalog(BaseModel):
    source_revision: str
    python_path: str = "."
    functions: list[CallableInfo] = Field(default_factory=list)
    classes: list[ClassInfo] = Field(default_factory=list)
    warnings: list[CatalogWarning] = Field(default_factory=list)

    def all_callables(self) -> list[CallableInfo]:
        result = list(self.functions)
        for item in self.classes:
            result.extend(item.methods)
        return sorted(result, key=lambda item: (-item.score, item.id))

    def callable_by_id(self, callable_id: str) -> CallableInfo | None:
        for item in self.all_callables():
            if item.id == callable_id:
                return item
        return None

    def class_by_target(self, target: str) -> ClassInfo | None:
        for item in self.classes:
            if item.target == target:
                return item
        return None


class OperatorParameterSpec(BaseModel):
    key: str
    display_name: str = ""
    description: str = ""
    required: bool = False
    has_default: bool = False
    default: Any | None = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("operator parameter key cannot be empty")
        return value


class ArgumentBinding(BaseModel):
    source: BindingSource
    codec: Codec = Codec.TEXT
    metadata_key: str | None = None
    parameter: OperatorParameterSpec | None = None
    constant: Any | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "ArgumentBinding":
        if self.source == BindingSource.INPUT_METADATA and not self.metadata_key:
            raise ValueError("input.metadata requires metadata_key")
        if self.source == BindingSource.OPERATOR_PARAMETER and self.parameter is None:
            raise ValueError("operator.parameter requires parameter")
        return self


class TargetRef(BaseModel):
    callable_id: str


class OperatorMetadata(BaseModel):
    name: str
    display_name: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("operator name cannot be empty")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
        if any(ch not in allowed for ch in value):
            raise ValueError("operator name may contain only letters, digits, '.', '_' and '-'")
        return value


class OutputValueSpec(BaseModel):
    source: OutputValueSource = OutputValueSource.RETURN
    path: list[str | int] = Field(default_factory=list)
    codec: Codec = Codec.BYTES
    value: Any | None = None

    @model_validator(mode="after")
    def validate_value(self) -> "OutputValueSpec":
        if self.source == OutputValueSource.RETURN_PATH and not self.path:
            raise ValueError("return.path output requires a non-empty path")
        return self


class OutputSpec(BaseModel):
    payload: OutputValueSpec = Field(default_factory=OutputValueSpec)
    metadata: dict[str, OutputValueSpec] = Field(default_factory=dict)
    none_policy: NonePolicy = NonePolicy.KEEP_ORIGINAL


class SourceSpec(BaseModel):
    source_revision: str
    python_path: str = "."
    source_ref: str | None = None

    @field_validator("python_path")
    @classmethod
    def validate_python_path(cls, value: str) -> str:
        value = value.strip() or "."
        if value.startswith("/") or "\\" in value:
            raise ValueError("python_path must be a relative POSIX path")
        parts = [part for part in value.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise ValueError("python_path must stay inside the source snapshot")
        return "/".join(parts) if parts else "."


class VirtualOperatorContract(BaseModel):
    api_version: Literal["dsc.virtual-operator/v1"] = "dsc.virtual-operator/v1"
    execution_model: Literal["direct"] = "direct"
    metadata: OperatorMetadata
    source: SourceSpec
    target: TargetRef
    constructor_arguments: dict[str, ArgumentBinding] = Field(default_factory=dict)
    arguments: dict[str, ArgumentBinding] = Field(default_factory=dict)
    output: OutputSpec = Field(default_factory=OutputSpec)
