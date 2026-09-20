from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from operator_authoring.snapshot import read_requirements

from .backends.models import RunnerBackendContract
from .backends.runner import write_runner_release_files
from .runtime_lifecycle import PublishRuntimeLifecycle
from .service import PublishError, PublishService


class LifecyclePublishService(PublishService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.runtime_lifecycle = PublishRuntimeLifecycle(self.store)

    def begin_runner_runtime_retirement(
        self,
        *,
        runtime_env_key: str,
        image_ref: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.runtime_lifecycle.begin_retirement(
            runtime_env_key=runtime_env_key,
            image_ref=image_ref,
            reason=reason,
        )

    def cancel_runner_runtime_retirement(
        self,
        *,
        runtime_env_key: str,
    ) -> dict[str, Any]:
        return self.runtime_lifecycle.cancel_retirement(
            runtime_env_key=runtime_env_key
        )

    def finalize_runner_runtime_retirement(
        self,
        *,
        runtime_env_key: str,
    ) -> dict[str, Any]:
        return self.runtime_lifecycle.finalize_retirement(
            runtime_env_key=runtime_env_key
        )

    def publish_backend(
        self,
        operator_id: str,
        backend: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        if backend != "runner":
            return super().publish_backend(operator_id, backend, options)

        variant, child, plan, source_path = self._compile_backend(
            operator_id,
            backend,
            options,
        )
        job = self.store.create_publish_job(variant["id"])
        self.store.mark_job_running(job["id"])

        try:
            result, artifact_ref = self._publish_runner_transactional(
                child,
                plan,
                source_path,
                job_id=str(job["id"]),
                variant_id=str(variant["id"]),
            )
            return {
                "jobId": str(job["id"]),
                "status": "READY",
                "backend": backend,
                "artifactRef": artifact_ref,
                "result": result,
            }
        except Exception as exc:
            self.store.mark_job_failed(
                job["id"],
                variant["id"],
                f"{type(exc).__name__}: {exc}",
            )
            raise

    def _publish_runner_transactional(
        self,
        child: RunnerBackendContract,
        plan,
        source_path: Path,
        *,
        job_id: str,
        variant_id: str,
    ) -> tuple[dict[str, Any], str]:
        requirements = read_requirements(source_path)
        runtime = self.build_client.resolve(requirements)

        if not runtime.get("found"):
            raise PublishError("Build Service did not return a runtime environment")
        if runtime.get("status") == "FAILED":
            raise PublishError(
                f"RUNTIME_BUILD_FAILED env_key={runtime.get('envKey')}: "
                f"{runtime.get('error')}"
            )
        if runtime.get("status") != "READY" or not runtime.get("image"):
            raise PublishError(
                f"RUNTIME_NOT_READY env_key={runtime.get('envKey')} "
                f"status={runtime.get('status')}"
            )

        runtime_env_key = runtime.get("runtimeEnvKey") or runtime.get("envKey")
        if not runtime_env_key:
            raise PublishError("Build Service did not return runtimeEnvKey/envKey")

        runtime_env_key = str(runtime_env_key)
        image_ref = str(runtime["image"])

        with self.runtime_lifecycle.guard(runtime_env_key):
            try:
                self.runtime_lifecycle.assert_active(runtime_env_key, image_ref)
            except RuntimeError as exc:
                raise PublishError(f"RUNTIME_RETIRING: {exc}") from exc

            with tempfile.TemporaryDirectory(prefix="mpr-runner-publish-") as temp:
                staging = Path(temp)
                shutil.copytree(source_path, staging / "runtime")
                write_runner_release_files(staging, contract=child, plan=plan)
                release = self.publisher.publish(
                    staging,
                    runtime_image=image_ref,
                    profile=child.runtime_profile,
                )

            self.runtime_lifecycle.assert_active(runtime_env_key, image_ref)

            result = {
                "releaseId": release.id,
                "runtimeImage": image_ref,
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
            artifact_ref = f"runner-release:{release.id}"

            self.store.mark_job_ready(
                job_id,
                variant_id,
                artifact_ref=artifact_ref,
                result=result,
            )

        return result, artifact_ref
