from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from operator_authoring.model import BindingSource, Codec, NonePolicy, OutputValueSource


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)


class AnalyzeRequest(ApiModel):
    workspace: str
    python_root: str | None = None


class BindingSelection(ApiModel):
    source: BindingSource
    codec: Codec = Codec.TEXT
    metadata_key: str | None = None
    parameter_key: str | None = None
    parameter_display_name: str | None = None
    parameter_description: str = ""
    parameter_required: bool = False
    parameter_has_default: bool = False
    parameter_default: Any | None = None
    constant: Any | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> "BindingSelection":
        if self.source == BindingSource.INPUT_METADATA and not self.metadata_key:
            raise ValueError("input.metadata requires metadataKey")
        if self.source == BindingSource.OPERATOR_PARAMETER and not self.parameter_key:
            raise ValueError("operator.parameter requires parameterKey")
        return self


class OutputValueSelection(ApiModel):
    source: OutputValueSource = OutputValueSource.RETURN
    path: list[str | int] = Field(default_factory=list)
    codec: Codec = Codec.BYTES
    value: Any | None = None


class OutputSelection(ApiModel):
    payload: OutputValueSelection = Field(default_factory=OutputValueSelection)
    metadata: dict[str, OutputValueSelection] = Field(default_factory=dict)
    none_policy: NonePolicy = NonePolicy.KEEP_ORIGINAL


class OperatorSelection(ApiModel):
    name: str
    display_name: str
    description: str = ""


class CreateVirtualContractRequest(ApiModel):
    workspace: str
    source_revision: str
    python_root: str
    operator: OperatorSelection
    callable_id: str
    constructor_bindings: dict[str, BindingSelection] = Field(default_factory=dict)
    argument_bindings: dict[str, BindingSelection] = Field(default_factory=dict)
    output: OutputSelection = Field(default_factory=OutputSelection)


class BackendRequest(ApiModel):
    options: dict[str, Any] = Field(default_factory=dict)
