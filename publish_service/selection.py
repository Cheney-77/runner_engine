from __future__ import annotations

from operator_authoring.model import (
    ArgumentBinding, OperatorMetadata, OperatorParameterSpec, OutputSpec,
    OutputValueSpec, SourceSpec, TargetRef, VirtualOperatorContract,
)

from .model import BindingSelection, CreateVirtualContractRequest


def _binding(value: BindingSelection) -> ArgumentBinding:
    parameter = None
    if value.source.value == "operator.parameter":
        parameter = OperatorParameterSpec(
            key=value.parameter_key or "",
            display_name=value.parameter_display_name or value.parameter_key or "",
            description=value.parameter_description,
            required=value.parameter_required,
            has_default=value.parameter_has_default,
            default=value.parameter_default,
        )

    return ArgumentBinding(
        source=value.source,
        codec=value.codec,
        metadata_key=value.metadata_key,
        parameter=parameter,
        constant=value.constant,
    )


def selection_to_virtual_contract(
    selection: CreateVirtualContractRequest,
    *,
    source_ref: str | None = None,
) -> VirtualOperatorContract:
    return VirtualOperatorContract(
        metadata=OperatorMetadata(
            name=selection.operator.name,
            display_name=selection.operator.display_name,
            description=selection.operator.description,
        ),
        source=SourceSpec(
            source_revision=selection.source_revision,
            python_path=selection.python_root,
            source_ref=source_ref,
        ),
        target=TargetRef(callable_id=selection.callable_id),
        constructor_arguments={
            name: _binding(binding)
            for name, binding in selection.constructor_bindings.items()
        },
        arguments={
            name: _binding(binding)
            for name, binding in selection.argument_bindings.items()
        },
        output=OutputSpec(
            payload=OutputValueSpec(
                source=selection.output.payload.source,
                path=selection.output.payload.path,
                codec=selection.output.payload.codec,
                value=selection.output.payload.value,
            ),
            metadata={
                name: OutputValueSpec(
                    source=item.source,
                    path=item.path,
                    codec=item.codec,
                    value=item.value,
                )
                for name, item in selection.output.metadata.items()
            },
            none_policy=selection.output.none_policy,
        ),
    )
