from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .errors import RunnerError
from .lifecycle_store import RuntimeLifecycleStore, runtime_id_for_image
from .pool import WorkerPool
from .sandbox.opensandbox import OpenSandboxBackend


class LifecycleWorkerPool(WorkerPool):
    def __init__(
        self,
        *args,
        lifecycle: RuntimeLifecycleStore,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.lifecycle = lifecycle
        self._lifecycle_gate_lock = threading.RLock()
        self._acquiring: dict[str, int] = {}

    def acquire(self, tenant_id, release, policy):
        runtime_id = release.runtime.id

        with self._lifecycle_gate_lock:
            if self.lifecycle.is_blocked_runtime(runtime_id):
                raise RunnerError(
                    "RUNTIME_RETIRING",
                    "runtime image is retiring and cannot acquire a sandbox",
                    retryable=True,
                )
            self._acquiring[runtime_id] = self._acquiring.get(runtime_id, 0) + 1

        try:
            return super().acquire(tenant_id, release, policy)
        finally:
            with self._lifecycle_gate_lock:
                remaining = self._acquiring.get(runtime_id, 0) - 1
                if remaining > 0:
                    self._acquiring[runtime_id] = remaining
                else:
                    self._acquiring.pop(runtime_id, None)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        workers = []

        with self._lock:
            for bucket in self._idle.values():
                for worker in bucket:
                    workers.append(
                        {
                            "workerId": worker.id,
                            "tenantId": worker.tenant_id,
                            "runtimeId": worker.runtime_id,
                            "profile": worker.profile,
                            "backendGroup": worker.backend_group,
                            "runs": worker.runs,
                            "healthy": worker.healthy,
                            "idleSeconds": max(
                                0.0,
                                now - worker.last_used_monotonic,
                            ),
                            "installedReleaseCount": len(
                                worker.installed_release_ids
                            ),
                        }
                    )

        with self._lifecycle_gate_lock:
            acquiring = dict(self._acquiring)

        return {
            "idleCount": len(workers),
            "idleWorkers": workers,
            "acquiringByRuntime": acquiring,
        }

    def retire_runtime(self, runtime_id: str) -> list[str]:
        retired = []

        with self._lock:
            for key, bucket in list(self._idle.items()):
                if key[1] != runtime_id:
                    continue
                retired.extend(bucket)
                self._idle.pop(key, None)

        for worker in retired:
            self._destroy(worker)

        return [worker.id for worker in retired]

    def retire_idle_worker(self, worker_id: str) -> bool:
        target = None

        with self._lock:
            for key, bucket in list(self._idle.items()):
                kept = []

                for worker in bucket:
                    if target is None and worker.id == worker_id:
                        target = worker
                    else:
                        kept.append(worker)

                if kept:
                    self._idle[key] = kept
                else:
                    self._idle.pop(key, None)

        if target is None:
            return False

        self._destroy(target)
        return True


class LifecycleOpenSandboxBackend(OpenSandboxBackend):
    def list_managed(
        self,
        *,
        runtime_id: str | None = None,
    ) -> list[dict[str, Any]]:
        result = []
        page_size = 100
        runtime_label = runtime_id[:32] if runtime_id else None

        for cluster in self.clusters:
            for page in range(1, 101):
                try:
                    response = self._request(
                        cluster,
                        "GET",
                        f"/v1/sandboxes?page={page}&pageSize={page_size}",
                    )
                except Exception:
                    break

                items = (
                    response
                    if isinstance(response, list)
                    else response.get("items") or response.get("sandboxes") or []
                )
                if not isinstance(items, list):
                    break

                for item in items:
                    metadata = item.get("metadata") or item.get("labels") or {}

                    if metadata.get("managed-by") != self.MANAGED_BY:
                        continue
                    if (
                        metadata.get("runner-owner")
                        != self._label_hash(self.owner_id)
                    ):
                        continue
                    if runtime_label and metadata.get("runtime") != runtime_label:
                        continue

                    try:
                        sandbox_id = self._sandbox_id(item)
                    except Exception:
                        continue

                    result.append(
                        {
                            "sandboxId": sandbox_id,
                            "cluster": cluster,
                            "state": self._state(item),
                            "runtime": metadata.get("runtime"),
                            "tenant": metadata.get("tenant"),
                            "profile": metadata.get("profile"),
                        }
                    )

                if len(items) < page_size:
                    break

        return result


class RunnerLifecycleController:
    def __init__(
        self,
        service,
        lifecycle: RuntimeLifecycleStore,
    ):
        self.service = service
        self.lifecycle = lifecycle

    def _release_rows(self, runtime_id: str) -> list[dict[str, str]]:
        rows = []
        catalog_root = Path(self.service.catalog.root)

        for child in catalog_root.iterdir():
            if not child.is_dir() or not (child / "release.json").is_file():
                continue

            try:
                release = self.service.catalog.get(child.name)
            except Exception:
                continue

            if release.runtime.id != runtime_id:
                continue

            rows.append(
                {
                    "releaseId": release.id,
                    "profile": release.profile,
                    "imageRef": release.runtime.image,
                }
            )

        return rows

    def _unexpired_lease_count(self, release_ids: list[str]) -> int:
        if not release_ids:
            return 0

        with self.service.db.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM runner.leases
                WHERE release_id = ANY(%s)
                  AND expires_at > now()
                """,
                (release_ids,),
            ).fetchone()

        return int(row["count"])

    def runtime_image_status(self, image_ref: str) -> dict[str, Any]:
        runtime_id = runtime_id_for_image(image_ref)
        lifecycle = self.lifecycle.get_by_runtime_id(runtime_id)
        releases = self._release_rows(runtime_id)
        release_ids = [item["releaseId"] for item in releases]

        with self.service._active_lock:
            active = [
                {
                    "invocationId": invocation_id,
                    "tenantId": item.tenant_id,
                    "leaseId": item.lease_id,
                    "releaseId": item.release_id,
                    "workerId": item.worker.id,
                }
                for invocation_id, item in self.service._active.items()
                if item.worker.runtime_id == runtime_id
            ]

        pool_snapshot = self.service.pool.snapshot()
        idle = [
            item
            for item in pool_snapshot["idleWorkers"]
            if item["runtimeId"] == runtime_id
        ]
        acquiring = int(
            pool_snapshot["acquiringByRuntime"].get(runtime_id, 0)
        )

        backend = self.service.pool.backend
        list_managed = getattr(backend, "list_managed", None)
        managed = (
            list_managed(runtime_id=runtime_id)
            if callable(list_managed)
            else []
        )

        state = lifecycle["state"] if lifecycle is not None else "ACTIVE"
        safe = (
            state == "RETIRING"
            and not active
            and not idle
            and acquiring == 0
            and not managed
        )

        return {
            "runtimeId": runtime_id,
            "imageRef": image_ref,
            "lifecycleState": state,
            "releaseCount": len(releases),
            "releases": releases[:100],
            "unexpiredLeaseCount": self._unexpired_lease_count(release_ids),
            "activeInvocationCount": len(active),
            "activeInvocations": active,
            "idleWorkerCount": len(idle),
            "idleWorkers": idle,
            "acquiringWorkerCount": acquiring,
            "managedSandboxCount": len(managed),
            "managedSandboxes": managed,
            "safeForArtifactDeletion": safe,
        }

    def begin_runtime_retirement(
        self,
        image_ref: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        runtime_id = runtime_id_for_image(image_ref)
        pool = self.service.pool

        with pool._lifecycle_gate_lock:
            row = self.lifecycle.begin_retirement(
                image_ref,
                reason=reason,
            )
            if row["state"] == "RETIRED":
                return self.runtime_image_status(image_ref)

            retired_workers = pool.retire_runtime(runtime_id)

        status = self.runtime_image_status(image_ref)
        status["retiredIdleWorkerIds"] = retired_workers
        return status

    def cancel_runtime_retirement(self, image_ref: str) -> dict[str, Any]:
        current = self.lifecycle.get_by_image(image_ref)
        if current is not None and current["state"] == "RETIRED":
            raise RuntimeError("a RETIRED runtime cannot be reactivated")

        pool = self.service.pool
        with pool._lifecycle_gate_lock:
            cancelled = self.lifecycle.cancel_retirement(image_ref)

        return {
            "cancelled": cancelled,
            "imageRef": image_ref,
            "lifecycleState": "ACTIVE",
        }

    def finalize_runtime_retirement(
        self,
        image_ref: str,
    ) -> dict[str, Any]:
        status = self.runtime_image_status(image_ref)
        if not status["safeForArtifactDeletion"]:
            raise RuntimeError(
                "runtime still has an active invocation, sandbox acquisition, "
                "idle worker or managed sandbox"
            )

        row = self.lifecycle.finalize_retirement(image_ref)
        if row is None:
            raise RuntimeError("runtime lifecycle row disappeared")

        return {
            **status,
            "lifecycleState": row["state"],
            "safeForArtifactDeletion": False,
        }

    def retire_idle_worker(self, worker_id: str) -> dict[str, Any]:
        retired = self.service.pool.retire_idle_worker(worker_id)
        if not retired:
            raise RuntimeError(
                "worker is not idle or is no longer owned by this Runner process"
            )

        return {
            "retired": True,
            "workerId": worker_id,
        }

    def runtime_snapshot(self) -> dict[str, Any]:
        with self.service._active_lock:
            active = [
                {
                    "invocationId": invocation_id,
                    "tenantId": item.tenant_id,
                    "leaseId": item.lease_id,
                    "releaseId": item.release_id,
                    "workerId": item.worker.id,
                    "runtimeId": item.worker.runtime_id,
                    "profile": item.worker.profile,
                }
                for invocation_id, item in self.service._active.items()
            ]

        pool = self.service.pool.snapshot()
        return {
            "activeInvocationCount": len(active),
            "activeInvocations": active,
            "idleWorkerCount": pool["idleCount"],
            "idleWorkers": pool["idleWorkers"],
            "acquiringByRuntime": pool["acquiringByRuntime"],
            "runtimeLifecycle": self.lifecycle.list_recent(100),
        }
