from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from operator_authoring.ast_scanner import scan_project
from operator_authoring.compiler import callable_support_reasons, compile_virtual_contract
from operator_authoring.inference import parameter_suggestions
from operator_authoring.model import CallableInfo, ParameterInfo, VirtualOperatorContract
from operator_authoring.snapshot import read_requirements
from runner_engine.catalog import Catalog
from runner_engine.publisher import OperatorPublisher

from .backends.base import backend_contract_sha256
from .backends.models import NifiNativeBackendContract, RunnerBackendContract
from .backends.nifi_native import (
    COMPILER_VERSION as NATIVE_COMPILER_VERSION,
    compile_nifi_native_contract,
    write_native_package,
)
from .backends.runner import (
    COMPILER_VERSION as RUNNER_COMPILER_VERSION,
    compile_runner_contract,
    write_runner_release_files,
)
from .build_client import BuildServiceClient
from .model import CreateVirtualContractRequest
from .selection import selection_to_virtual_contract
from .source_store import LocalSourceStore


logger = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublishSettings:
    workspace_root: Path
    catalog_root: Path
    source_root: Path
    native_artifact_root: Path
    database_url: str
    build_service_url: str
    default_profile: str = "standard"
    build_timeout_seconds: int = 1800
    max_source_bytes: int = 20 * 1024 * 1024


class PublishService:
    def __init__(
        self,
        settings: PublishSettings,
        *,
        store: Any | None = None,
        source_store: LocalSourceStore | None = None,
        build_client: BuildServiceClient | None = None,
    ):
        self.settings = settings
        self.settings.workspace_root.mkdir(parents=True, exist_ok=True)
        self.settings.catalog_root.mkdir(parents=True, exist_ok=True)
        self.settings.source_root.mkdir(parents=True, exist_ok=True)
        self.settings.native_artifact_root.mkdir(parents=True, exist_ok=True)

        if store is None:
            from .store import PublishStore
            self.store = PublishStore(settings.database_url)
        else:
            self.store = store
        self.source_store = source_store or LocalSourceStore(
            settings.source_root,
            max_source_bytes=settings.max_source_bytes,
        )
        self.build_client = build_client or BuildServiceClient(
            settings.build_service_url,
            timeout_seconds=settings.build_timeout_seconds,
        )
        self.publisher = OperatorPublisher(Catalog(settings.catalog_root))

    def close(self) -> None:
        self.store.close()

    def _workspace(self, workspace: str) -> Path:
        if not workspace or workspace.startswith("/"):
            raise PublishError("workspace must be a non-empty relative path")

        root = self.settings.workspace_root.resolve()
        candidate = (root / workspace).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise PublishError("workspace escapes PUBLISH_WORKSPACE_ROOT") from exc
        if not candidate.is_dir():
            raise PublishError(f"workspace does not exist: {workspace}")
        return candidate

    @staticmethod
    def _python_root_candidates(root: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for candidate in ("src", "."):
            path = root if candidate == "." else root / candidate
            if not path.is_dir() or not any(path.rglob("*.py")):
                continue
            result.append({"path": candidate, "recommended": candidate == "src"})
        if result and not any(item["recommended"] for item in result):
            result[0]["recommended"] = True
        return result

    def _select_python_root(self, root: Path, requested: str | None) -> tuple[str, list[dict[str, Any]]]:
        candidates = self._python_root_candidates(root)
        if not candidates:
            raise PublishError("workspace does not contain Python source files")

        if requested is not None:
            if requested not in {item["path"] for item in candidates}:
                raise PublishError(f"pythonRoot is not available: {requested}")
            return requested, candidates

        recommended = next((item["path"] for item in candidates if item["recommended"]), candidates[0]["path"])
        return recommended, candidates

    @staticmethod
    def _parameter_view(parameter: ParameterInfo, *, index: int, constructor: bool) -> dict[str, Any]:
        return {
            "name": parameter.name,
            "kind": parameter.kind,
            "annotation": parameter.annotation,
            "required": parameter.required,
            "hasLiteralDefault": parameter.has_default,
            "defaultValue": parameter.default_value if parameter.has_default else None,
            "defaultExpression": parameter.default_repr,
            "allowedSources": (
                ["operator.parameter", "constant"]
                if constructor
                else ["input.payload", "input.metadata", "operator.parameter", "constant"]
            ),
            "suggestions": parameter_suggestions(parameter, index=index, constructor=constructor),
        }

    def _callable_view(self, item: CallableInfo, catalog) -> dict[str, Any]:
        reasons = callable_support_reasons(item, catalog)
        constructor = []

        if item.class_target and item.kind.value == "instance-method":
            class_info = catalog.class_by_target(item.class_target)
            if class_info is not None:
                constructor = [
                    self._parameter_view(parameter, index=index, constructor=True)
                    for index, parameter in enumerate(class_info.constructor)
                ]

        return {
            "id": item.id,
            "kind": item.kind.value,
            "displayName": item.qualname,
            "file": item.file,
            "line": item.line,
            "supported": not reasons,
            "unsupportedReasons": reasons,
            "constructorParameters": constructor,
            "parameters": [
                self._parameter_view(parameter, index=index, constructor=False)
                for index, parameter in enumerate(item.parameters)
            ],
            "returnAnnotation": item.return_annotation,
            "score": item.score,
        }

    def analyze(self, workspace: str, *, python_root: str | None = None) -> dict[str, Any]:
        root = self._workspace(workspace)
        selected_root, candidates = self._select_python_root(root, python_root)
        catalog = scan_project(root, python_path=selected_root)

        callables = [self._callable_view(item, catalog) for item in catalog.all_callables()]
        supported = [item for item in callables if item["supported"]]
        return {
            "workspace": workspace,
            "sourceRevision": catalog.source_revision,
            "pythonRoot": selected_root,
            "pythonRootCandidates": candidates,
            "recommendedCallableId": supported[0]["id"] if supported else None,
            "callables": callables,
            "warnings": [item.model_dump(mode="json") for item in catalog.warnings],
        }

    def create_virtual_contract(self, selection: CreateVirtualContractRequest) -> dict[str, Any]:
        workspace = self._workspace(selection.workspace)
        mutable_catalog = scan_project(workspace, python_path=selection.python_root)

        if mutable_catalog.source_revision != selection.source_revision:
            raise PublishError(
                "SOURCE_CHANGED: workspace changed after Analyze; analyze again before saving the contract"
            )

        draft = selection_to_virtual_contract(selection)
        compile_virtual_contract(draft, mutable_catalog)

        source_ref = self.source_store.put_workspace(
            workspace,
            expected_revision=selection.source_revision,
        )
        immutable_source = self.source_store.resolve(source_ref)
        immutable_catalog = scan_project(immutable_source, python_path=selection.python_root)

        parent = selection_to_virtual_contract(selection, source_ref=source_ref)
        plan = compile_virtual_contract(parent, immutable_catalog)
        operator, contract_row, created = self.store.save_virtual_contract(selection.workspace, parent)

        return {
            "created": created,
            "operatorId": str(operator["id"]),
            "contractId": str(contract_row["id"]),
            "contractVersion": contract_row["version"],
            "contractSha256": contract_row["contract_sha256"],
            "sourceRevision": contract_row["source_revision"],
            "sourceRef": contract_row["source_ref"],
            "virtualContract": parent.model_dump(mode="json"),
            "derived": {
                "parameters": [item.model_dump(mode="json") for item in plan.parameters],
                "inputMetadata": plan.input_metadata,
                "outputMetadata": plan.output_metadata,
            },
        }

    def get_operator(self, operator_id: str) -> dict[str, Any]:
        operator = self.store.get_operator(operator_id)
        if operator is None:
            raise PublishError("operator not found")

        contract = self.store.get_latest_contract(operator_id)
        if contract is None:
            raise PublishError("operator has no contract versions")

        variants = self.store.list_variants(contract["id"])
        return {
            "operatorId": str(operator["id"]),
            "workspace": operator["workspace"],
            "name": operator["name"],
            "displayName": operator["display_name"],
            "description": operator["description"],
            "currentContract": {
                "contractId": str(contract["id"]),
                "version": contract["version"],
                "sourceRevision": contract["source_revision"],
                "sourceRef": contract["source_ref"],
                "contractSha256": contract["contract_sha256"],
                "virtualContract": contract["contract_json"],
            },
            "backendVariants": [self._variant_view(item) for item in variants],
        }

    @staticmethod
    def _variant_view(row) -> dict[str, Any]:
        return {
            "variantId": str(row["id"]),
            "backend": row["backend"],
            "compilerVersion": row["compiler_version"],
            "variantKey": row["variant_key"],
            "status": row["status"],
            "publishedRef": row["published_ref"],
            "publishedMetadata": row["published_metadata"],
            "lastError": row["last_error"],
            "backendContract": row["backend_contract_json"],
        }

    def _load_current_parent(self, operator_id: str):
        row = self.store.get_latest_contract(operator_id)
        if row is None:
            raise PublishError("operator or contract not found")

        parent = VirtualOperatorContract.model_validate(row["contract_json"])
        if not parent.source.source_ref:
            raise PublishError("virtual contract has no immutable source_ref")

        source_path = self.source_store.resolve(parent.source.source_ref)
        catalog = scan_project(source_path, python_path=parent.source.python_path)
        plan = compile_virtual_contract(parent, catalog)
        return row, parent, plan, source_path

    def _compile_backend(self, operator_id: str, backend: str, options: dict[str, Any]):
        row, parent, plan, source_path = self._load_current_parent(operator_id)

        if backend == "runner":
            normalized_options = {"profile": str(options.get("profile", self.settings.default_profile))}
            child = compile_runner_contract(
                contract_id=str(row["id"]),
                contract_version=row["version"],
                parent=parent,
                plan=plan,
                options=normalized_options,
            )
            compiler_version = RUNNER_COMPILER_VERSION
        elif backend == "nifi_native":
            child = compile_nifi_native_contract(
                contract_id=str(row["id"]),
                contract_version=row["version"],
                parent=parent,
                plan=plan,
                source_path=source_path,
                options=options,
            )
            normalized_options = child.backend_options
            compiler_version = NATIVE_COMPILER_VERSION
        else:
            raise PublishError(f"unsupported backend: {backend}")

        digest = backend_contract_sha256(child)
        variant = self.store.upsert_variant(
            contract_id=str(row["id"]),
            backend=backend,
            compiler_version=compiler_version,
            variant_key=child.variant_key,
            options=normalized_options,
            backend_contract_sha256=digest,
            backend_contract=child.model_dump(mode="json"),
        )
        return variant, child, plan, source_path

    def compile_backend(self, operator_id: str, backend: str, options: dict[str, Any]) -> dict[str, Any]:
        variant, child, _, _ = self._compile_backend(operator_id, backend, options)
        return {
            **self._variant_view(variant),
            "backendContractSha256": variant["backend_contract_sha256"],
            "parentContractId": child.parent_contract_id,
            "parentContractVersion": child.parent_contract_version,
            "parentContractSha256": child.parent_contract_sha256,
        }

    def publish_backend(self, operator_id: str, backend: str, options: dict[str, Any]) -> dict[str, Any]:
        variant, child, plan, source_path = self._compile_backend(operator_id, backend, options)
        job = self.store.create_publish_job(variant["id"])
        self.store.mark_job_running(job["id"])
        logging.info(f"source_path:{source_path}")

        try:
            if backend == "runner":
                result, artifact_ref = self._publish_runner(child, plan, source_path)
            elif backend == "nifi_native":
                result, artifact_ref = self._publish_native(child, plan, source_path)
            else:
                raise PublishError(f"unsupported backend: {backend}")

            self.store.mark_job_ready(
                job["id"],
                variant["id"],
                artifact_ref=artifact_ref,
                result=result,
            )
            return {
                "jobId": str(job["id"]),
                "status": "READY",
                "backend": backend,
                "artifactRef": artifact_ref,
                "result": result,
            }
        except Exception as exc:
            self.store.mark_job_failed(job["id"], variant["id"], f"{type(exc).__name__}: {exc}")
            raise

    def _publish_runner(
        self,
        child: RunnerBackendContract,
        plan,
        source_path: Path,
    ) -> tuple[dict[str, Any], str]:
        requirements = read_requirements(source_path)
        runtime = self.build_client.resolve(requirements)

        if not runtime.get("found"):
            raise PublishError("Build Service did not return a runtime environment")
        if runtime.get("status") == "FAILED":
            raise PublishError(
                f"RUNTIME_BUILD_FAILED env_key={runtime.get('envKey')}: {runtime.get('error')}"
            )
        if runtime.get("status") != "READY" or not runtime.get("image"):
            raise PublishError(
                f"RUNTIME_NOT_READY env_key={runtime.get('envKey')} status={runtime.get('status')}"
            )

        with tempfile.TemporaryDirectory(prefix="mpr-runner-publish-") as temp:
            staging = Path(temp)
            shutil.copytree(source_path, staging / "runtime")
            write_runner_release_files(staging, contract=child, plan=plan)
            release = self.publisher.publish(
                staging,
                runtime_image=runtime["image"],
                profile=child.runtime_profile,
            )

        result = {
            "releaseId": release.id,
            "runtimeImage": runtime["image"],
            "environmentId": runtime.get("environmentId"),
            "envKey": runtime.get("envKey"),
            "runtimeEnvKey": runtime.get("runtimeEnvKey"),
            "resolutionKind": runtime.get("resolutionKind"),
            "processorDefinition": {
                "processorType": "ManagedPythonTransform",
                "inputAttributes": child.runner_manifest.input_attributes,
                "outputAttributes": child.runner_manifest.output_attributes,
                "parameters": [item.model_dump(mode="json") for item in child.parameters],
            },
        }
        return result, f"runner-release:{release.id}"

    def _publish_native(
        self,
        child: NifiNativeBackendContract,
        plan,
        source_path: Path,
    ) -> tuple[dict[str, Any], str]:
        artifact = write_native_package(
            self.settings.native_artifact_root,
            contract=child,
            plan=plan,
            source_path=source_path,
        )
        result = {
            "packageName": child.package_name,
            "className": child.class_name,
            "processorType": child.processor_type,
            "artifactFile": str(artifact),
            "deploymentRequired": True,
            "deploymentHint": (
                "Deploy the unpacked package to a configured NiFi Python extension source directory."
            ),
        }
        return result, f"nifi-native-package:{artifact.name}"
