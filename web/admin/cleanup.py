from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lifecycle_client import (
    LifecycleClient,
    LifecycleRejected,
    LifecycleUnavailable,
)


@dataclass
class CleanupCoordinator:
    inventory: Any
    store: Any
    lifecycle: LifecycleClient
    actor: str

    def plan(self, environment_id: str) -> dict[str, Any]:
        preview = self.inventory.cleanup_preview(environment_id)
        if not preview.get("found"):
            return {
                "environmentId": environment_id,
                "status": preview.get("status", "not_found"),
                "canExecute": False,
                "blockingReasons": preview.get("blockingReasons", []),
                "inventory": preview,
            }

        build = self.lifecycle.build_plan(environment_id)
        image_ref = build.get("imageRef") or preview["environment"].get("imageRef")
        blockers = list(preview.get("blockingReasons") or [])

        if build.get("locallyBlocked"):
            blockers.append("Build Service reports active build work.")
        if not image_ref and build.get("lifecycleState") != "RETIRED":
            blockers.append("Runtime environment has no immutable image_ref.")

        runner = self.lifecycle.runner_status(image_ref) if image_ref else None
        return {
            "environmentId": environment_id,
            "status": "ready" if not blockers else "blocked",
            "canExecute": self.lifecycle.enabled and not blockers,
            "blockingReasons": blockers,
            "warnings": preview.get("warnings") or [],
            "inventory": preview,
            "build": build,
            "runner": runner,
        }

    def execute(self, request_id: str) -> dict[str, Any]:
        request = self.store.get_cleanup_request(request_id)
        if request is None:
            raise ValueError("cleanup request not found")
        if request["status"] != "DRAFT":
            raise ValueError("cleanup request must remain in DRAFT state")

        environment_id = request["environmentId"]
        execution_id = self.store.start_cleanup_execution(
            self.actor,
            request_id,
            environment_id,
        )

        try:
            plan = self.plan(environment_id)
        except Exception as exc:
            self.store.finish_cleanup_execution(
                self.actor,
                execution_id,
                "FAILED",
                f"planning failed: {type(exc).__name__}: {exc}",
            )
            raise

        self.store.record_cleanup_step(
            execution_id,
            "preflight",
            "SUCCEEDED" if plan["canExecute"] else "BLOCKED",
            plan,
        )
        if not plan["canExecute"]:
            self.store.finish_cleanup_execution(
                self.actor,
                execution_id,
                "BLOCKED",
                "fresh preflight has blockers",
            )
            return {
                "executionId": execution_id,
                "status": "BLOCKED",
                "plan": plan,
            }

        environment = plan["inventory"]["environment"]
        env_key = environment["envKey"]
        reason = request["reason"]
        image_ref = (
            plan["build"].get("imageRef")
            or environment.get("imageRef")
            or request.get("imageRef")
        )
        if not image_ref:
            self.store.finish_cleanup_execution(
                self.actor,
                execution_id,
                "FAILED",
                "cleanup request has no image_ref",
            )
            raise ValueError("cleanup request has no image_ref")

        gates_started = []
        artifact_deleted = False

        try:
            build = self.lifecycle.build_retire(environment_id, reason=reason)
            gates_started.append("build")
            self.store.record_cleanup_step(
                execution_id,
                "build.retire",
                "SUCCEEDED",
                build,
            )

            publish = self.lifecycle.publish_begin_retirement(
                runtime_env_key=env_key,
                image_ref=image_ref,
                reason=reason,
            )
            gates_started.append("publish")
            publish_safe = bool(publish.get("safeForRetirement"))
            self.store.record_cleanup_step(
                execution_id,
                "publish.retire",
                "SUCCEEDED" if publish_safe else "BLOCKED",
                publish,
            )

            if not publish_safe:
                rollback = self._rollback(
                    environment_id,
                    env_key,
                    image_ref,
                    gates_started,
                )
                self.store.record_cleanup_step(
                    execution_id,
                    "rollback",
                    "SUCCEEDED" if not rollback["errors"] else "FAILED",
                    rollback,
                )
                self.store.finish_cleanup_execution(
                    self.actor,
                    execution_id,
                    "BLOCKED",
                    "CURRENT published Runner variants still reference this runtime",
                )
                return {
                    "executionId": execution_id,
                    "status": "BLOCKED",
                    "publish": publish,
                }

            runner = self.lifecycle.runner_begin_retirement(
                image_ref,
                reason=reason,
            )
            gates_started.append("runner")
            self.store.record_cleanup_step(
                execution_id,
                "runner.retire",
                "SUCCEEDED",
                runner,
            )

            if not runner.get("safeForArtifactDeletion"):
                self.store.record_cleanup_step(
                    execution_id,
                    "runner.drain",
                    "WAITING",
                    runner,
                )
                self.store.finish_cleanup_execution(
                    self.actor,
                    execution_id,
                    "WAITING",
                    "runtime is gated; wait for active work/sandboxes to drain",
                )
                return {
                    "executionId": execution_id,
                    "status": "WAITING",
                    "message": (
                        "Retirement gates are active. New publish/lease/invoke "
                        "cannot use this image. Retry after active work drains."
                    ),
                    "runner": runner,
                }

            lifecycle_state = (
                build.get("lifecycle_state")
                or build.get("lifecycleState")
            )
            if lifecycle_state != "RETIRED":
                deleted = self.lifecycle.build_delete_artifact(
                    environment_id,
                    expected_image_ref=image_ref,
                )
                artifact_deleted = True
                self.store.record_cleanup_step(
                    execution_id,
                    "harbor.delete-artifact",
                    "SUCCEEDED",
                    deleted,
                )
            else:
                artifact_deleted = True

            runner_done = self.lifecycle.runner_finalize_retirement(image_ref)
            self.store.record_cleanup_step(
                execution_id,
                "runner.finalize",
                "SUCCEEDED",
                runner_done,
            )

            publish_done = self.lifecycle.publish_finalize_retirement(
                runtime_env_key=env_key
            )
            self.store.record_cleanup_step(
                execution_id,
                "publish.finalize",
                "SUCCEEDED",
                publish_done,
            )

            self.store.finish_cleanup_execution(
                self.actor,
                execution_id,
                "SUCCEEDED",
                "runtime environment retired and Harbor artifact removed",
            )
            return {
                "executionId": execution_id,
                "status": "SUCCEEDED",
                "environmentId": environment_id,
                "envKey": env_key,
                "imageRef": image_ref,
                "garbageCollectionRequired": True,
            }

        except (
            LifecycleRejected,
            LifecycleUnavailable,
            RuntimeError,
            ValueError,
        ) as exc:
            self.store.record_cleanup_step(
                execution_id,
                "cleanup.error",
                "FAILED",
                {
                    "errorType": type(exc).__name__,
                    "message": str(exc),
                    "artifactDeleted": artifact_deleted,
                },
            )

            if artifact_deleted:
                self.store.finish_cleanup_execution(
                    self.actor,
                    execution_id,
                    "PARTIAL",
                    (
                        "artifact deletion succeeded but finalization failed; "
                        "lifecycle gates intentionally remain closed"
                    ),
                )
            else:
                rollback = self._rollback(
                    environment_id,
                    env_key,
                    image_ref,
                    gates_started,
                )
                self.store.record_cleanup_step(
                    execution_id,
                    "rollback",
                    "SUCCEEDED" if not rollback["errors"] else "FAILED",
                    rollback,
                )
                self.store.finish_cleanup_execution(
                    self.actor,
                    execution_id,
                    "FAILED",
                    str(exc),
                )
            raise

    def _rollback(
        self,
        environment_id: str,
        env_key: str,
        image_ref: str,
        gates_started: list[str],
    ) -> dict[str, Any]:
        errors = []
        restored = []

        if "runner" in gates_started:
            try:
                self.lifecycle.runner_cancel_retirement(image_ref)
                restored.append("runner")
            except Exception as exc:
                errors.append(f"runner: {type(exc).__name__}: {exc}")

        if "publish" in gates_started:
            try:
                self.lifecycle.publish_cancel_retirement(
                    runtime_env_key=env_key
                )
                restored.append("publish")
            except Exception as exc:
                errors.append(f"publish: {type(exc).__name__}: {exc}")

        if "build" in gates_started:
            try:
                self.lifecycle.build_cancel_retirement(environment_id)
                restored.append("build")
            except Exception as exc:
                errors.append(f"build: {type(exc).__name__}: {exc}")

        return {"restored": restored, "errors": errors}
