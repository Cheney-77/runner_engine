from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from operator_authoring.model import VirtualOperatorContract


class BackendContractBase(VirtualOperatorContract):
    backend_contract_version: Literal["dsc.backend-contract/v1"] = "dsc.backend-contract/v1"
    parent_contract_id: str
    parent_contract_version: int
    parent_contract_sha256: str
    backend: str
    compiler_version: str
    variant_key: str
    backend_options: dict[str, Any] = Field(default_factory=dict)


class RunnerManifest(BaseModel):
    name: str
    entrypoint: str
    input_attributes: list[str] = Field(default_factory=list)
    output_attributes: list[str] = Field(default_factory=list)


class RunnerParameter(BaseModel):
    key: str
    nifi_property_name: str
    display_name: str
    description: str = ""
    required: bool = False
    has_default: bool = False
    default: Any | None = None


class RunnerBackendContract(BackendContractBase):
    backend: Literal["runner"] = "runner"
    runtime_profile: str = "standard"
    runner_manifest: RunnerManifest
    parameters: list[RunnerParameter] = Field(default_factory=list)


class NativeProperty(BaseModel):
    key: str
    display_name: str
    description: str = ""
    required: bool = False
    has_default: bool = False
    default: Any | None = None


class NifiNativeBackendContract(BackendContractBase):
    backend: Literal["nifi_native"] = "nifi_native"
    processor_type: Literal["FlowFileTransform"] = "FlowFileTransform"
    class_name: str
    package_name: str
    properties: list[NativeProperty] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    deployment_mode: Literal["package_only"] = "package_only"
