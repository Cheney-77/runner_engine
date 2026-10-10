from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .builder import build_runtime_image, lock_requirements, verify_runtime_image
from .model import RuntimeBuildSpec
from .store import BuildStore


logger = logging.getLogger(__name__)

ProgressCallback = Callable[
    [str, str, dict[str, Any] | None],
    None,
]


@dataclass(frozen=True)
class BuildSettings:
    base_image: str
    registry_repo: str
    python_version: str = "3.12"
    platform: str = "linux/amd64"
    uv_python_platform: str = "x86_64-unknown-linux-gnu"
    build_policy_version: str = "1"
    uv_default_index: str | None = None
    allow_superset_reuse: bool = True


class BuildService:
    def __init__(self, store: BuildStore, settings: BuildSettings):
        self.store = store
        self.settings = settings

    @staticmethod
    def _emit(
        progress: ProgressCallback | None,
        stage: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if progress is not None:
            progress(stage, message, detail)

    def resolve(
        self,
        requirements: list[str],
        *,
        progress: ProgressCallback | None = None,
    ):
        logger.info(
            "Resolving runtime environment requirements=%d python=%s platform=%s policy=%s",
            len(requirements),
            self.settings.python_version,
            self.settings.platform,
            self.settings.build_policy_version,
        )
        self._emit(
            progress,
            "RESOLVING_DEPENDENCIES",
            "正在解析 Runner Runtime 的 Python 依赖",
            {"directRequirementCount": len(requirements)},
        )

        lock_text = lock_requirements(
            requirements,
            python_version=self.settings.python_version,
            python_platform=self.settings.uv_python_platform,
            uv_default_index=self.settings.uv_default_index,
        )
        spec = RuntimeBuildSpec(
            base_image=self.settings.base_image,
            python_version=self.settings.python_version,
            platform=self.settings.platform,
            requirements_lock=lock_text,
            build_policy_version=self.settings.build_policy_version,
        )
        request_key = spec.env_key

        logger.info(
            "Resolved dependency lock env_key=%s packages=%d lock_sha256=%s",
            request_key,
            len(spec.packages),
            spec.lock_sha256,
        )
        self._emit(
            progress,
            "CHECKING_RUNTIME_CACHE",
            "依赖锁定完成，正在检查可复用的 Runtime 环境",
            {
                "envKey": request_key,
                "packageCount": len(spec.packages),
                "lockSha256": spec.lock_sha256,
            },
        )

        existing = self.store.get_for_request(request_key)
        if existing is not None:
            logger.info(
                "Found existing runtime environment env_key=%s env_id=%s status=%s resolution=%s",
                request_key,
                existing["id"],
                existing["status"],
                existing.get("resolution_kind", "exact"),
            )

            if existing["status"] == "PENDING":
                return self._build(
                    request_key,
                    existing,
                    spec,
                    progress=progress,
                )

            if existing["status"] == "READY":
                self._emit(
                    progress,
                    "REUSING_RUNTIME",
                    "已找到可复用的 Runner Runtime 环境",
                    {
                        "envKey": request_key,
                        "environmentId": str(existing["id"]),
                        "resolutionKind": existing.get("resolution_kind", "exact"),
                    },
                )
                self._emit(
                    progress,
                    "RUNTIME_READY",
                    "Runner Runtime 环境已就绪",
                    {"envKey": request_key},
                )
            elif existing["status"] in {"BUILDING", "VERIFYING"}:
                self._emit(
                    progress,
                    "WAITING_RUNTIME",
                    "相同 Runtime 环境正在由其他任务构建，等待其完成",
                    {
                        "envKey": request_key,
                        "status": existing["status"],
                    },
                )
            elif existing["status"] == "FAILED":
                logger.warning(
                    "Runtime environment is FAILED env_key=%s env_id=%s; "
                    "use POST /v1/runtime-environments/%s/retry after fixing the cause",
                    request_key,
                    existing["id"],
                    request_key,
                )
                self._emit(
                    progress,
                    "RUNTIME_FAILED",
                    "已有 Runner Runtime 环境处于 FAILED",
                    {
                        "envKey": request_key,
                        "error": existing.get("last_error"),
                    },
                )

            return existing

        if self.settings.allow_superset_reuse:
            candidate = self.store.find_smallest_superset(spec)
            if candidate is not None:
                self.store.bind_request(
                    request_key,
                    candidate["id"],
                    "superset",
                )
                logger.info(
                    "Reusing compatible runtime superset request_env_key=%s env_id=%s runtime_env_key=%s packages=%d",
                    request_key,
                    candidate["id"],
                    candidate["env_key"],
                    candidate["package_count"],
                )
                reused = self.store.get_for_request(request_key)
                self._emit(
                    progress,
                    "REUSING_RUNTIME",
                    "已找到满足依赖要求的 Runtime 超集环境",
                    {
                        "envKey": request_key,
                        "runtimeEnvKey": candidate["env_key"],
                        "environmentId": str(candidate["id"]),
                        "resolutionKind": "superset",
                    },
                )
                self._emit(
                    progress,
                    "RUNTIME_READY",
                    "Runner Runtime 环境已就绪",
                    {"envKey": request_key},
                )
                return reused

        env = self.store.get_or_create_exact(spec)
        self.store.bind_request(request_key, env["id"], "exact")

        logger.info(
            "Using exact runtime environment env_key=%s env_id=%s status=%s",
            request_key,
            env["id"],
            env["status"],
        )

        if env["status"] == "PENDING":
            return self._build(
                request_key,
                env,
                spec,
                progress=progress,
            )

        if env["status"] == "READY":
            self._emit(
                progress,
                "REUSING_RUNTIME",
                "Exact Runtime 环境已经存在，直接复用",
                {
                    "envKey": request_key,
                    "environmentId": str(env["id"]),
                },
            )
            self._emit(
                progress,
                "RUNTIME_READY",
                "Runner Runtime 环境已就绪",
                {"envKey": request_key},
            )
        elif env["status"] in {"BUILDING", "VERIFYING"}:
            self._emit(
                progress,
                "WAITING_RUNTIME",
                "相同 Runtime 环境正在构建，等待其完成",
                {
                    "envKey": request_key,
                    "status": env["status"],
                },
            )
        elif env["status"] == "FAILED":
            logger.warning(
                "Exact runtime environment is FAILED env_key=%s env_id=%s; retry explicitly after fixing the cause",
                request_key,
                env["id"],
            )
            self._emit(
                progress,
                "RUNTIME_FAILED",
                "Runner Runtime 环境构建失败",
                {
                    "envKey": request_key,
                    "error": env.get("last_error"),
                },
            )

        return self.store.get_for_request(request_key)

    def retry(self, request_env_key: str):
        existing = self.store.get_for_request(request_env_key)
        if existing is None:
            raise KeyError(request_env_key)

        if existing["status"] != "FAILED":
            raise ValueError(
                f"runtime environment status is {existing['status']}; only FAILED environments can be retried"
            )

        env = self.store.reset_failed(existing["id"])
        if env is None:
            current = self.store.get_for_request(request_env_key)
            if current is None:
                raise KeyError(request_env_key)
            return current

        spec = RuntimeBuildSpec(
            base_image=env["base_image"],
            python_version=env["python_version"],
            platform=env["platform"],
            requirements_lock=env["requirements_lock"],
            build_policy_version=env["build_policy_version"],
        )

        logger.info(
            "Retrying failed runtime environment request_env_key=%s env_id=%s runtime_env_key=%s",
            request_env_key,
            env["id"],
            env["env_key"],
        )
        return self._build(request_env_key, env, spec)

    def get(self, env_key: str):
        return self.store.get_for_request(env_key)

    def _build(
        self,
        request_key: str,
        env,
        spec: RuntimeBuildSpec,
        *,
        progress: ProgressCallback | None = None,
    ):
        job_id = self.store.claim_build(env["id"])
        if job_id is None:
            current = self.store.get_for_request(request_key)
            logger.info(
                "Build was not claimed env_key=%s env_id=%s current_status=%s",
                request_key,
                env["id"],
                None if current is None else current["status"],
            )
            self._emit(
                progress,
                "WAITING_RUNTIME",
                "Runtime 构建已被其他并发请求接管，等待现有任务完成",
                {
                    "envKey": request_key,
                    "status": None if current is None else current["status"],
                },
            )
            return current

        logger.info(
            "Build started env_key=%s env_id=%s job_id=%s packages=%d",
            request_key,
            env["id"],
            job_id,
            len(spec.packages),
        )
        self._emit(
            progress,
            "BUILDING_RUNTIME",
            "正在构建并推送 Runner Runtime 镜像",
            {
                "envKey": request_key,
                "environmentId": str(env["id"]),
                "buildJobId": str(job_id),
                "packageCount": len(spec.packages),
            },
        )

        try:
            image_ref = build_runtime_image(
                spec,
                registry_repo=self.settings.registry_repo,
                build_tag=f"env-{env['id']}",
            )

            logger.info(
                "Image build/push complete env_id=%s job_id=%s image=%s",
                env["id"],
                job_id,
                image_ref,
            )

            self.store.mark_verifying(env["id"], job_id)
            logger.info(
                "Runtime verification started env_id=%s job_id=%s",
                env["id"],
                job_id,
            )
            self._emit(
                progress,
                "VERIFYING_RUNTIME",
                "Runtime 镜像已推送，正在验证其中的 Python 依赖",
                {
                    "envKey": request_key,
                    "image": image_ref,
                    "packageCount": len(spec.packages),
                },
            )

            verify_runtime_image(image_ref, spec.packages)

            self.store.mark_ready(env["id"], job_id, image_ref)
            logger.info(
                "Runtime environment READY env_key=%s env_id=%s job_id=%s image=%s",
                request_key,
                env["id"],
                job_id,
                image_ref,
            )
            self._emit(
                progress,
                "RUNTIME_READY",
                "Runner Runtime 环境构建并验证完成",
                {
                    "envKey": request_key,
                    "image": image_ref,
                },
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "Runtime environment build FAILED env_key=%s env_id=%s job_id=%s",
                request_key,
                env["id"],
                job_id,
            )
            self.store.mark_failed(env["id"], job_id, message)
            self._emit(
                progress,
                "RUNTIME_FAILED",
                "Runner Runtime 环境构建失败",
                {
                    "envKey": request_key,
                    "error": message,
                },
            )

        return self.store.get_for_request(request_key)
