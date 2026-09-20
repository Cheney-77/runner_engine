from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from operator_authoring.adapter_generator import generate_runner_adapter
from operator_authoring.compiler import ExecutionPlan, canonical_json
from operator_authoring.model import VirtualOperatorContract

from .base import backend_variant_key
from .models import RunnerBackendContract, RunnerManifest, RunnerParameter


# v4 semantics:
# - pythonRoot is both import root and user working directory.
# - Agent may provide a disposable writable runtime view via RUNNER_RUNTIME_ROOT.
COMPILER_VERSION = "runner-contract-v4"


def compile_runner_contract(
    *,
    contract_id: str,
    contract_version: int,
    parent: VirtualOperatorContract,
    plan: ExecutionPlan,
    options: dict[str, Any],
) -> RunnerBackendContract:
    profile = str(options.get("profile", "standard"))
    variant_key = backend_variant_key(
        contract_sha256=plan.contract_sha256,
        backend="runner",
        compiler_version=COMPILER_VERSION,
        options={"profile": profile},
    )

    return RunnerBackendContract(
        **parent.model_dump(mode="python"),
        parent_contract_id=contract_id,
        parent_contract_version=contract_version,
        parent_contract_sha256=plan.contract_sha256,
        compiler_version=COMPILER_VERSION,
        variant_key=variant_key,
        backend_options={"profile": profile},
        runtime_profile=profile,
        runner_manifest=RunnerManifest(
            name=parent.metadata.name,
            entrypoint="__dsc_entry__:process",
            input_attributes=plan.input_metadata,
            output_attributes=plan.output_metadata,
        ),
        parameters=[
            RunnerParameter(
                key=item.key,
                nifi_property_name=f"Parameter.{item.key}",
                display_name=item.display_name,
                description=item.description,
                required=item.required,
                has_default=item.has_default,
                default=item.default,
            )
            for item in plan.parameters
        ],
    )


def write_runner_release_files(
    staging_root: str | Path,
    *,
    contract: RunnerBackendContract,
    plan: ExecutionPlan,
) -> None:
    root = Path(staging_root)

    (root / "operator-contract.yaml").write_text(
        yaml.safe_dump(
            contract.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    (root / "__dsc_entry__.py").write_text(
        generate_runner_adapter(plan),
        encoding="utf-8",
    )

    (root / "operator.yaml").write_text(
        yaml.safe_dump(
            contract.runner_manifest.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    publish_info = {
        "schema": 3,
        "backend": "runner",
        "parent_contract_id": contract.parent_contract_id,
        "parent_contract_version": contract.parent_contract_version,
        "parent_contract_sha256": contract.parent_contract_sha256,
        "variant_key": contract.variant_key,
        "execution_plan_sha256": hashlib.sha256(
            canonical_json(plan.model_dump(mode="json"))
        ).hexdigest(),
        "execution_plan_embedded_in": "__dsc_entry__.py",
    }
    (root / "publish-info.json").write_bytes(canonical_json(publish_info))
