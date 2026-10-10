from __future__ import annotations

import json
import logging
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    supported_native_targets,
    write_native_package,
)
from .backends.runner import (
    COMPILER_VERSION as RUNNER_COMPILER_VERSION,
    compile_runner_contract,
    write_runner_release_files,
)
from .build_client import BuildServiceClient
from .edge_bundle import _file_hashes, build_edge_bundle
from .edge_dependencies import EdgeDependencyResolutionError, EdgeDependencyResolver
from .edge_identity import edge_directory_name
from .model import CreateVirtualContractRequest
from .progress import PublishProgressReporter
from .selection import selection_to_virtual_contract
from .source_store import LocalSourceStore

logger = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


class EdgeDependencyConflictError(PublishError):
    def __init__(self, detail: dict[str, Any]):
        super().__init__(detail["message"])
        self.detail = detail


@dataclass(frozen=True)
class PublishSettings:
    workspace_root: Path
    catalog_root: Path
    source_root: Path
    native_artifact_root: Path
    edge_bundle_root: Path
    database_url: str
    build_service_url: str
    default_profile: str = "standard"
    # default_user_id: str = "1"
    edge_python_version: str = "3.12"
    edge_uv_default_index: str | None = None
    edge_require_binary: bool = True
    build_timeout_seconds: int = 1800
    publish_worker_threads: int = 4
    max_source_bytes: int = 500 * 1024 * 1024
    auth_mode: str = "required"
    demo_user_id: str | None = None
    jwt_secret: str | None = None


class PublishService:
    def __init__(
            self,
            settings: PublishSettings,
            *,
            store: Any | None = None,
            source_store: LocalSourceStore | None = None,
            build_client: BuildServiceClient | None = None,
            edge_resolver: EdgeDependencyResolver | None = None,
    ):
        self.settings = settings

        for path in (
                settings.workspace_root,
                settings.catalog_root,
                settings.source_root,
                settings.native_artifact_root,
                settings.edge_bundle_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

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
        self.edge_resolver = edge_resolver or EdgeDependencyResolver(
            uv_default_index=settings.edge_uv_default_index,
            require_binary=settings.edge_require_binary,
        )
        self.publisher = OperatorPublisher(Catalog(settings.catalog_root))
        self._publish_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.publish_worker_threads),
            thread_name_prefix="publish-job",
        )

    def close(self) -> None:
        # Do not close the DB pool while accepted publish jobs are still using it.
        # FastAPI lifespan calls close() during graceful shutdown.
        self._publish_executor.shutdown(
            wait=True,
            cancel_futures=False,
        )
        self.store.close()

    def get_publish_job(
            self,
            job_id: str,
            *,
            user_id: str,
    ) -> dict[str, Any]:
        row = self.store.get_publish_job(job_id, user_id=user_id)
        if row is None:
            raise PublishError("publish job not found")

        result_json = row.get("result_json")
        progress = None
        result = None

        if isinstance(result_json, dict):
            if row["status"] in {"PENDING", "RUNNING"}:
                candidate = result_json.get("_progress")
                if isinstance(candidate, dict):
                    progress = candidate
            elif row["status"] == "READY":
                result = result_json

        return {
            "jobId": str(row["id"]),
            "status": row["status"],
            "backend": row["backend"],
            "variantId": str(row["backend_variant_id"]),
            "operatorId": str(row["operator_id"]),
            "artifactRef": row["artifact_ref"],
            "progress": progress,
            "result": result,
            "error": row["error_message"],
            "createdAt": row["created_at"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
        }

    def edge_platforms(self) -> dict[str, Any]:
        return {
            "pythonVersion": self.settings.edge_python_version,
            "platforms": supported_native_targets(self.settings.edge_python_version),
        }

    def edge_deployments(self, *, user_id: str) -> dict[str, Any]:
        rows = self.store.list_edge_deployments_for_user(user_id=user_id)
        return {
            "userId": user_id,
            "deployments": [
                {
                    "operatorId": str(row["operator_id"]),
                    "variantId": str(row["variant_id"]),
                    "workspace": row["workspace"],
                    "name": row["name"],
                    "displayName": row["display_name"],
                    "packageName": row["package_name"],
                    "edgeIdentity": {
                        "tokenPair": row["token_pair"], "edgeName": row["edge_name"]
                    },
                    "requirements": list(row["requirements_json"]),
                    "targetPlatform": {
                        "os": row["target_os"],
                        "arch": row["target_arch"],
                        "pythonVersion": row["python_version"],
                        "uvPythonPlatform": row["uv_python_platform"],
                    },
                    "createdAt": row["created_at"],
                    "updatedAt": row["updated_at"],
                }
                for row in rows
            ],
        }

    def edge_bundles(self, *, user_id: str, limit: int = 50) -> dict[str, Any]:
        rows = self.store.list_edge_bundles(user_id=user_id, limit=limit)
        return {
            "userId": user_id,
            "bundles": [
                {
                    "bundleId": str(row["id"]),
                    "revision": row["revision"],
                    "edgeIdentity": {
                        "tokenPair": row["token_pair"], "edgeName": row["edge_name"]
                    },
                    "targetPlatform": {
                        "os": row["target_os"],
                        "arch": row["target_arch"],
                        "pythonVersion": row["python_version"],
                        "uvPythonPlatform": row["uv_python_platform"],
                    },
                    "requirements": list(row["requirements_json"]),
                    "lockSha256": row["lock_sha256"],
                    "artifactRef": row["artifact_ref"],
                    "artifactSha256": row["artifact_sha256"],
                    "manifest": row["manifest_json"],
                    "createdAt": row["created_at"],
                }
                for row in rows
            ],
        }

    def edge_operations(self, *, user_id: str, limit: int = 100) -> dict[str, Any]:
        rows = self.store.list_edge_operations(user_id=user_id, limit=limit)
        return {
            "userId": user_id,
            "operations": [
                {
                    "jobId": str(row["job_id"]),
                    "jobStatus": row["job_status"],
                    "variantId": str(row["variant_id"]),
                    "variantStatus": row["variant_status"],
                    "operatorId": str(row["operator_id"]),
                    "workspace": row["workspace"],
                    "name": row["name"],
                    "displayName": row["display_name"],
                    "options": row["options_json"],
                    "edgeIdentity": row["options_json"].get("edgeIdentity"),
                    "artifactRef": row["artifact_ref"],
                    "publishedMetadata": row["published_metadata"],
                    "errorMessage": row["error_message"],
                    "createdAt": row["created_at"],
                    "startedAt": row["started_at"],
                    "finishedAt": row["finished_at"],
                }
                for row in rows
            ],
        }

    def edge_bundle_file(self, bundle_id: str, *, user_id: str) -> Path:
        row = self.store.get_edge_bundle(user_id=user_id, bundle_id=bundle_id)
        if row is None:
            raise PublishError("edge bundle not found")
        return self._edge_bundle_path_from_row(row)

    def _edge_bundle_path_from_row(self, row) -> Path:
        """Resolve a persisted Bundle without opening another DB connection."""
        prefix = "edge-native-bundle:"
        artifact_ref = str(row["artifact_ref"])
        if not artifact_ref.startswith(prefix):
            raise PublishError("edge bundle artifact reference is invalid")

        filename = artifact_ref[len(prefix):]
        if not filename or Path(filename).name != filename:
            raise PublishError("edge bundle artifact filename is invalid")

        target_key = (
            f'{row["target_os"]}-{row["target_arch"]}-py{row["python_version"]}'
        )
        root = self.settings.edge_bundle_root.resolve()
        target_root = root / f'user-{row["user_id"]}' / target_key
        if row["token_pair"] is not None and row["edge_name"] is not None:
            target_root /= edge_directory_name(row["token_pair"], row["edge_name"])
        path = (target_root / filename).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PublishError("edge bundle path escapes configured root") from exc

        if not path.is_file():
            raise PublishError("edge bundle artifact file is missing")

        return path

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
        result = []

        for candidate in ("src", "."):
            path = root if candidate == "." else root / candidate

            if not path.is_dir() or not any(path.rglob("*.py")):
                continue

            result.append(
                {
                    "path": candidate,
                    "recommended": candidate == "src",
                }
            )

        if result and not any(item["recommended"] for item in result):
            result[0]["recommended"] = True

        return result

    def _select_python_root(
            self,
            root: Path,
            requested: str | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        candidates = self._python_root_candidates(root)

        if not candidates:
            raise PublishError("workspace does not contain Python source files")

        if requested is not None:
            if requested not in {item["path"] for item in candidates}:
                raise PublishError(f"pythonRoot is not available: {requested}")
            return requested, candidates

        recommended = next(
            (
                item["path"]
                for item in candidates
                if item["recommended"]
            ),
            candidates[0]["path"],
        )
        return recommended, candidates

    @staticmethod
    def _parameter_view(
            parameter: ParameterInfo,
            *,
            index: int,
            constructor: bool,
    ) -> dict[str, Any]:
        return {
            "name": parameter.name,
            "kind": parameter.kind,
            "annotation": parameter.annotation,
            "required": parameter.required,
            "hasLiteralDefault": parameter.has_default,
            "defaultValue": (
                parameter.default_value
                if parameter.has_default
                else None
            ),
            "defaultExpression": parameter.default_repr,
            "allowedSources": (
                ["operator.parameter", "constant"]
                if constructor
                else [
                    "input.payload",
                    "input.metadata",
                    "operator.parameter",
                    "constant",
                ]
            ),
            "suggestions": parameter_suggestions(
                parameter,
                index=index,
                constructor=constructor,
            ),
        }

    def _callable_view(self, item: CallableInfo, catalog) -> dict[str, Any]:
        reasons = callable_support_reasons(item, catalog)
        constructor = []

        if item.class_target and item.kind.value == "instance-method":
            class_info = catalog.class_by_target(item.class_target)

            if class_info is not None:
                constructor = [
                    self._parameter_view(
                        parameter,
                        index=index,
                        constructor=True,
                    )
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
                self._parameter_view(
                    parameter,
                    index=index,
                    constructor=False,
                )
                for index, parameter in enumerate(item.parameters)
            ],
            "returnAnnotation": item.return_annotation,
            "score": item.score,
        }

    def analyze(
            self,
            workspace: str,
            *,
            python_root: str | None = None,
    ) -> dict[str, Any]:
        root = self._workspace(workspace)
        selected_root, candidates = self._select_python_root(root, python_root)
        catalog = scan_project(root, python_path=selected_root)

        callables = [
            self._callable_view(item, catalog)
            for item in catalog.all_callables()
        ]
        supported = [item for item in callables if item["supported"]]

        return {
            "workspace": workspace,
            "sourceRevision": catalog.source_revision,
            "pythonRoot": selected_root,
            "pythonRootCandidates": candidates,
            "recommendedCallableId": (
                supported[0]["id"]
                if supported
                else None
            ),
            "callables": callables,
            "warnings": [
                item.model_dump(mode="json")
                for item in catalog.warnings
            ],
        }

    def create_virtual_contract(
            self,
            selection: CreateVirtualContractRequest,
            *,
            user_id: str,
    ) -> dict[str, Any]:
        workspace = self._workspace(selection.workspace)
        mutable_catalog = scan_project(
            workspace,
            python_path=selection.python_root,
        )

        if mutable_catalog.source_revision != selection.source_revision:
            raise PublishError(
                "SOURCE_CHANGED: workspace changed after Analyze; "
                "analyze again before saving the contract"
            )

        draft = selection_to_virtual_contract(selection)
        compile_virtual_contract(draft, mutable_catalog)

        source_ref = self.source_store.put_workspace(
            workspace,
            expected_revision=selection.source_revision,
        )
        immutable_source = self.source_store.resolve(source_ref)
        immutable_catalog = scan_project(
            immutable_source,
            python_path=selection.python_root,
        )

        parent = selection_to_virtual_contract(
            selection,
            source_ref=source_ref,
        )
        plan = compile_virtual_contract(parent, immutable_catalog)
        print("*****"*50)
        print(plan.model_dump_json())

        operator, contract_row, created = self.store.save_virtual_contract(
            selection.workspace,
            parent,
            user_id=user_id,
        )

        return {
            "created": created,
            "userId": user_id,
            "operatorId": str(operator["id"]),
            "contractId": str(contract_row["id"]),
            "contractVersion": contract_row["version"],
            "contractSha256": contract_row["contract_sha256"],
            "sourceRevision": contract_row["source_revision"],
            "sourceRef": contract_row["source_ref"],
            "virtualContract": parent.model_dump(mode="json"),
            "derived": {
                "parameters": [
                    item.model_dump(mode="json")
                    for item in plan.parameters
                ],
                "inputMetadata": plan.input_metadata,
                "outputMetadata": plan.output_metadata,
            },
        }

    def get_operator(self, operator_id: str, *, user_id: str) -> dict[str, Any]:
        operator = self.store.get_operator(operator_id, user_id=user_id)

        if operator is None:
            raise PublishError("operator not found")

        contract = self.store.get_latest_contract(operator_id, user_id=user_id)
        if contract is None:
            raise PublishError("operator has no contract versions")

        variants = self.store.list_variants(contract["id"])

        return {
            "userId": operator["user_id"],
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
            "backendVariants": [
                self._variant_view(item)
                for item in variants
            ],
        }

    def get_operator_list(self, run_type: str, user_id: str) -> dict[str, Any]:

        if run_type not in ("runner", "edge"):
            raise PublishError("run_type not defined")
        if not user_id:
            raise PublishError("authenticated user_id is required")
        operator_list = self.store.get_operator_list(run_type, user_id)

        if operator_list is None:
            raise PublishError("operator not found")

        return operator_list

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

    def _load_current_parent(self, operator_id: str, *, user_id: str):
        row = self.store.get_latest_contract(operator_id, user_id=user_id)

        if row is None:
            raise PublishError("operator or contract not found")

        parent = VirtualOperatorContract.model_validate(row["contract_json"])

        if not parent.source.source_ref:
            raise PublishError("virtual contract has no immutable source_ref")

        source_path = self.source_store.resolve(parent.source.source_ref)
        catalog = scan_project(
            source_path,
            python_path=parent.source.python_path,
        )
        plan = compile_virtual_contract(parent, catalog)

        return row, parent, plan, source_path

    def _compile_backend(
            self,
            operator_id: str,
            backend: str,
            options: dict[str, Any],
            *,
            user_id: str,
    ):
        row, parent, plan, source_path = self._load_current_parent(operator_id, user_id=user_id)
        if backend == "runner":
            normalized_options = {
                "profile": str(
                    options.get(
                        "profile",
                        self.settings.default_profile,
                    )
                )
            }
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
                default_python_version=self.settings.edge_python_version,
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

    def compile_backend(
            self,
            operator_id: str,
            backend: str,
            options: dict[str, Any],
            *,
            user_id: str,
    ) -> dict[str, Any]:
        variant, child, _, _ = self._compile_backend(
            operator_id,
            backend,
            options,
            user_id=user_id,
        )

        return {
            **self._variant_view(variant),
            "backendContractSha256": variant["backend_contract_sha256"],
            "parentContractId": child.parent_contract_id,
            "parentContractVersion": child.parent_contract_version,
            "parentContractSha256": child.parent_contract_sha256,
        }

    def publish_backend(
            self,
            operator_id: str,
            backend: str,
            options: dict[str, Any],
            *,
            user_id: str,
    ) -> dict[str, Any]:
        variant, child, plan, source_path = self._compile_backend(
            operator_id,
            backend,
            options,
            user_id=user_id,
        )
        job = self.store.create_publish_job(variant["id"])
        reporter = PublishProgressReporter(
            self.store,
            str(job["id"]),
            backend,
        )
        reporter.emit(
            "QUEUED",
            "发布任务已创建，等待后台 Worker 执行",
            status="PENDING",
        )

        try:
            self._publish_executor.submit(
                self._execute_publish_job,
                operator_id,
                backend,
                job,
                variant,
                child,
                plan,
                source_path,
                user_id,
                options.get("forceRebuild") is True,
                reporter,
            )
        except Exception as exc:
            self.store.mark_job_failed(
                job["id"],
                variant["id"],
                f"{type(exc).__name__}: {exc}",
            )
            raise

        return {
            "jobId": str(job["id"]),
            "status": "PENDING",
            "backend": backend,
            "statusUrl": f"/v1/publish-jobs/{job['id']}",
            "eventsUrl": f"/v1/publish-jobs/{job['id']}/events",
        }

    def _execute_publish_job(
            self,
            operator_id: str,
            backend: str,
            job,
            variant,
            child,
            plan,
            source_path: Path,
            user_id: str,
            force_rebuild: bool,
            progress: PublishProgressReporter,
    ) -> None:
        try:
            self.store.mark_job_running(job["id"])
            progress.start_heartbeat()
            progress.emit(
                "PREPARING",
                "后台 Worker 已开始执行发布任务",
            )

            if backend == "runner":
                self._publish_runner_job(
                    child,
                    plan,
                    source_path,
                    job_id=str(job["id"]),
                    variant_id=str(variant["id"]),
                    progress=progress,
                )
            elif backend == "nifi_native":
                # Native keeps its existing transactional READY behavior:
                # - bundle reuse -> mark_job_ready(..., conn=edge_conn)
                # - new bundle -> commit_edge_publish(..., conn=edge_conn)
                self._publish_native(
                    operator_id,
                    job["id"],
                    variant,
                    child,
                    plan,
                    source_path,
                    user_id=user_id,
                    force_rebuild=force_rebuild,
                    progress=progress,
                )
            else:
                raise PublishError(f"unsupported backend: {backend}")

        except EdgeDependencyConflictError as exc:
            self.store.mark_job_failed(
                job["id"],
                variant["id"],
                json.dumps(
                    exc.detail,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            logger.warning(
                "Publish job failed with dependency conflict: job_id=%s backend=%s",
                job["id"],
                backend,
            )

        except Exception as exc:
            self.store.mark_job_failed(
                job["id"],
                variant["id"],
                f"{type(exc).__name__}: {exc}",
            )
            logger.exception(
                "Publish job failed: job_id=%s backend=%s",
                job["id"],
                backend,
            )
        finally:
            progress.stop_heartbeat()

    def _publish_runner_job(
            self,
            child: RunnerBackendContract,
            plan,
            source_path: Path,
            *,
            job_id: str,
            variant_id: str,
            progress: PublishProgressReporter,
    ) -> tuple[dict[str, Any], str]:
        """Publish one Runner job and mark it READY.

        LifecyclePublishService overrides this hook so the runtime lifecycle
        advisory lock can remain held through mark_job_ready().
        """
        result, artifact_ref = self._publish_runner(
            child,
            plan,
            source_path,
            progress=progress,
        )
        self.store.mark_job_ready(
            job_id,
            variant_id,
            artifact_ref=artifact_ref,
            result=result,
        )
        return result, artifact_ref

    def _publish_runner(
            self,
            child: RunnerBackendContract,
            plan,
            source_path: Path,
            *,
            progress: PublishProgressReporter,
    ) -> tuple[dict[str, Any], str]:
        progress.emit(
            "READING_REQUIREMENTS",
            "正在读取 Runner 算子的 Python requirements",
        )
        requirements = read_requirements(source_path)
        runtime = self.build_client.resolve(
            requirements,
            progress=lambda stage, message, detail=None: progress.emit(
                stage,
                message,
                detail=detail,
            ),
        )

        if not runtime.get("found"):
            raise PublishError(
                "Build Service did not return a runtime environment"
            )

        if runtime.get("status") == "FAILED":
            raise PublishError(
                "RUNTIME_BUILD_FAILED "
                f"env_key={runtime.get('envKey')}: "
                f"{runtime.get('error')}"
            )

        if runtime.get("status") != "READY" or not runtime.get("image"):
            raise PublishError(
                "RUNTIME_NOT_READY "
                f"env_key={runtime.get('envKey')} "
                f"status={runtime.get('status')}"
            )

        progress.emit(
            "BUILDING_PACKAGE",
            "Runner Runtime 已就绪，正在生成 Release 文件",
            detail={
                "envKey": runtime.get("envKey"),
                "runtimeEnvKey": runtime.get("runtimeEnvKey"),
            },
        )
        with tempfile.TemporaryDirectory(
                prefix="mpr-runner-publish-"
        ) as temp:
            staging = Path(temp)
            shutil.copytree(source_path, staging / "runtime")

            write_runner_release_files(
                staging,
                contract=child,
                plan=plan,
            )
            progress.emit(
                "PERSISTING",
                "正在写入 Runner Release 与 Catalog",
            )
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
                "parameters": [
                    item.model_dump(mode="json")
                    for item in child.parameters
                ],
            },
        }

        return result, f"runner-release:{release.id}"

    @staticmethod
    def _edge_member_from_row(row) -> dict[str, Any]:
        return {
            "operator_id": str(row["operator_id"]),
            "variant_id": str(row["variant_id"]),
            "package_name": row["package_name"],
            "artifact_file": row["artifact_file"],
            "requirements": list(row["requirements_json"]),
        }

    def _dependency_conflict_detail(
            self,
            *,
            operator_id: str,
            child: NifiNativeBackendContract,
            members: list[dict[str, Any]],
            exc: EdgeDependencyResolutionError,
    ) -> dict[str, Any]:
        return {
            "code": "EDGE_DEPENDENCY_CONFLICT",
            "message": (
                "The new NiFi Native operator cannot share one dependency "
                "environment with the operators already published to this edge target."
            ),
            "edgeIdentity": child.backend_options["edgeIdentity"],
            "targetPlatform": {
                "os": child.target_platform.os,
                "arch": child.target_platform.arch,
                "pythonVersion": child.target_platform.python_version,
                "uvPythonPlatform": child.target_platform.uv_python_platform,
            },
            "candidate": {
                "operatorId": operator_id,
                "packageName": child.package_name,
                "requirements": list(child.requirements),
            },
            "environmentMembers": [
                {
                    "operatorId": item["operator_id"],
                    "packageName": item["package_name"],
                    "requirements": list(item["requirements"]),
                }
                for item in members
                if item["operator_id"] != operator_id
            ],
            "resolverError": exc.stderr or exc.stdout or str(exc),
        }

    @staticmethod
    def _same_edge_members(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> bool:
        """Compare the complete machine environment, not only the requested operator."""

        def signature(member):
            requirements = member.get("requirements_json")
            if requirements is None:
                requirements = member.get("requirements") or []
            return (
                str(member["operator_id"]), str(member["variant_id"]),
                str(member["package_name"]), tuple(requirements),
            )

        return sorted(map(signature, expected)) == sorted(map(signature, actual))

    @staticmethod
    def _native_publish_result(
            child: NifiNativeBackendContract, *, native_artifact: Path,
            bundle_id: str, revision: int, bundle_file: Path, bundle_ref: str,
            bundle_sha256: str, bundle_md5: str, lock_sha256: str,
            package_count: int, processor_count: int, reused: bool,
    ) -> dict[str, Any]:
        target = child.target_platform
        return {
            "packageName": child.package_name,
            "className": child.class_name,
            "processorType": child.processor_type,
            "artifactFile": str(native_artifact),
            "deploymentRequired": True,
            "deploymentMode": child.deployment_mode,
            "edgeIdentity": child.backend_options["edgeIdentity"],
            "targetPlatform": {
                "os": target.os,
                "arch": target.arch,
                "pythonVersion": target.python_version,
                "uvPythonPlatform": target.uv_python_platform,
            },
            "edgeBundle": {
                "bundleId": bundle_id,
                "revision": revision,
                "artifactFile": str(bundle_file),
                "artifactRef": bundle_ref,
                "artifactSha256": bundle_sha256,
                "artifactMd5": bundle_md5,
                "dependencyLockSha256": lock_sha256,
                "dependencyPackageCount": package_count,
                "processorCount": processor_count,
            },
            "reusedExistingBundle": reused,
            "deploymentHint": (
                "Deploy this full edge bundle atomically. It contains the shared "
                "target-platform dependency layer and every currently active "
                "NiFi Native processor for this user, target, and edge device."
            ),
        }

    def _publish_native(
            self,
            operator_id: str,
            job_id: str,
            variant,
            child: NifiNativeBackendContract,
            plan,
            source_path: Path,
            *,
            user_id: str,
            force_rebuild: bool = False,
            progress: PublishProgressReporter,
    ) -> tuple[dict[str, Any], str]:
        target = child.target_platform
        edge_identity = child.backend_options["edgeIdentity"]
        token_pair, edge_name = edge_identity["tokenPair"], edge_identity["edgeName"]
        device = {
            "user_id": user_id,
            "target_os": target.os,
            "target_arch": target.arch,
            "python_version": target.python_version,
            "token_pair": token_pair,
            "edge_name": edge_name,
        }

        progress.emit(
            "WAITING_LOCK",
            "正在等待目标边端机器的发布锁",
            detail={
                "edgeIdentity": edge_identity,
                "targetPlatform": {
                    "os": target.os,
                    "arch": target.arch,
                    "pythonVersion": target.python_version,
                    "uvPythonPlatform": target.uv_python_platform,
                },
            },
        )

        # Keep the lookup, comparison, and commit under the same device-scoped lock.
        with self.store.edge_publish_lock(**device) as edge_conn:
            progress.emit(
                "CHECKING_BUNDLE_REUSE",
                "已获取边端发布锁，正在检查现有 Bundle 是否可以直接复用",
                detail={"edgeIdentity": edge_identity},
            )
            current_rows = self.store.list_edge_deployments(**device, conn=edge_conn)
            current = next((row for row in current_rows if str(row["operator_id"]) == str(operator_id)), None)
            members = [
                self._edge_member_from_row(row)
                for row in current_rows if str(row["operator_id"]) != str(operator_id)
            ]
            candidate = {
                "operator_id": str(operator_id),
                "variant_id": str(variant["id"]),
                "package_name": child.package_name,
                "requirements": list(child.requirements),
            }
            expected_members = members + [candidate]

            if not force_rebuild and current is not None:
                latest = self.store.get_latest_edge_bundle(**device, conn=edge_conn)
                if latest is not None and latest["uv_python_platform"] == target.uv_python_platform:
                    latest_members = self.store.list_edge_bundle_members(bundle_id=latest["id"], conn=edge_conn)
                    current_matches = self._same_edge_members([candidate], [current])
                    if current_matches and self._same_edge_members(expected_members, latest_members):
                        native_file = Path(current["artifact_file"])
                        progress.emit(
                            "VERIFYING_ARTIFACT",
                            "发布内容未变化，正在校验已有 Edge Bundle 完整性",
                            detail={
                                "bundleId": str(latest["id"]),
                                "revision": latest["revision"],
                            },
                        )
                        try:
                            bundle_file = self._edge_bundle_path_from_row(latest)
                            bundle_sha256, bundle_md5 = _file_hashes(bundle_file)
                        except (PublishError, OSError):
                            logger.warning("Existing edge bundle is unavailable; rebuilding: bundle_id=%s",
                                           latest["id"])
                        else:
                            if native_file.is_file() and bundle_sha256 == latest["artifact_sha256"]:
                                manifest = latest["manifest_json"]
                                bundle_ref = str(latest["artifact_ref"])
                                result = self._native_publish_result(
                                    child, native_artifact=native_file,
                                    bundle_id=str(latest["id"]), revision=latest["revision"],
                                    bundle_file=bundle_file, bundle_ref=bundle_ref,
                                    bundle_sha256=bundle_sha256, bundle_md5=bundle_md5,
                                    lock_sha256=latest["lock_sha256"],
                                    package_count=manifest.get("dependencyPackageCount", 0),
                                    processor_count=len(latest_members), reused=True,
                                )
                                progress.emit(
                                    "PERSISTING",
                                    "已有 Bundle 校验通过，直接复用现有发布产物",
                                    detail={
                                        "bundleId": str(latest["id"]),
                                        "revision": latest["revision"],
                                        "reusedExistingBundle": True,
                                    },
                                )
                                self.store.mark_job_ready(
                                    job_id, str(variant["id"]), artifact_ref=bundle_ref,
                                    result=result, conn=edge_conn,
                                )
                                return result, bundle_ref
                            logger.warning(
                                "Existing edge artifacts are missing or corrupted; rebuilding: bundle_id=%s",
                                latest["id"]
                            )

            # Cache miss: regenerate the processor ZIP and the complete shared environment.
            progress.emit(
                "BUILDING_PACKAGE",
                "现有 Bundle 无法复用，正在生成 NiFi Native Processor 包",
                detail={"forceRebuild": force_rebuild},
            )
            artifact = write_native_package(
                self.settings.native_artifact_root, contract=child,
                plan=plan, source_path=source_path,
            )
            members.append({**candidate, "artifact_file": str(artifact)})
            all_requirements = [req for member in members for req in member["requirements"]]
            progress.emit(
                "RESOLVING_DEPENDENCIES",
                "正在解析该边端机器完整环境的共享 Python 依赖",
                detail={
                    "directRequirementCount": len(all_requirements),
                    "processorCount": len(members),
                },
            )
            try:
                resolution = self.edge_resolver.resolve(all_requirements, target=target)
            except EdgeDependencyResolutionError as exc:
                raise EdgeDependencyConflictError(
                    self._dependency_conflict_detail(
                        operator_id=operator_id, child=child, members=members, exc=exc,
                    )
                ) from exc

            revision = self.store.next_edge_bundle_revision(**device, conn=edge_conn)
            progress.emit(
                "BUILDING_BUNDLE",
                "依赖解析完成，正在生成完整 Edge Bundle",
                detail={
                    "revision": revision,
                    "dependencyPackageCount": resolution.package_count,
                    "processorCount": len(members),
                },
            )
            try:
                bundle_file, bundle_sha256, bundle_md5, manifest = build_edge_bundle(
                    self.settings.edge_bundle_root, user_id=user_id, target=target,
                    token_pair=token_pair, edge_name=edge_name, revision=revision,
                    members=members, resolution=resolution, resolver=self.edge_resolver,
                    progress=lambda stage, message, detail=None: progress.emit(
                        stage,
                        message,
                        detail=detail,
                    ),
                )
            except EdgeDependencyResolutionError as exc:
                raise EdgeDependencyConflictError(
                    self._dependency_conflict_detail(
                        operator_id=operator_id, child=child, members=members, exc=exc,
                    )
                ) from exc

            bundle_ref = f"edge-native-bundle:{bundle_file.name}"
            bundle_id = str(uuid.uuid4())
            result = self._native_publish_result(
                child, native_artifact=artifact, bundle_id=bundle_id, revision=revision,
                bundle_file=bundle_file, bundle_ref=bundle_ref,
                bundle_sha256=bundle_sha256, bundle_md5=bundle_md5,
                lock_sha256=resolution.lock_sha256,
                package_count=resolution.package_count,
                processor_count=len(members), reused=False,
            )
            progress.emit(
                "PERSISTING",
                "正在保存 Edge Bundle、部署成员和发布结果",
                detail={
                    "bundleId": bundle_id,
                    "revision": revision,
                    "reusedExistingBundle": False,
                },
            )
            self.store.commit_edge_publish(
                user_id=user_id, operator_id=operator_id, variant_id=str(variant["id"]),
                bundle_id=bundle_id, job_id=job_id, published_result=result,
                target_os=target.os, target_arch=target.arch,
                python_version=target.python_version, token_pair=token_pair,
                edge_name=edge_name, uv_python_platform=target.uv_python_platform,
                package_name=child.package_name, native_artifact_file=str(artifact),
                candidate_requirements=list(child.requirements),
                bundle_revision=revision,
                bundle_requirements=list(resolution.direct_requirements),
                requirements_lock=resolution.lock_text,
                lock_sha256=resolution.lock_sha256, bundle_artifact_ref=bundle_ref,
                bundle_artifact_sha256=bundle_sha256, manifest=manifest,
                members=members, conn=edge_conn,
            )

        return result, bundle_ref
