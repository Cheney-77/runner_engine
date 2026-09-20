from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException

from .cleanup import CleanupCoordinator
from .execution_store import CleanupExecutionStore
from .lifecycle_client import (
    LifecycleClient,
    LifecycleRejected,
    LifecycleUnavailable,
)
from .lifecycle_inventory import LifecycleInventory


def install_lifecycle_routes(
    app: FastAPI,
    *,
    inventory,
    store,
    audit_actor: str,
    lifecycle_client: LifecycleClient | None = None,
) -> None:
    lifecycle = lifecycle_client or LifecycleClient.environment()
    execution_store = CleanupExecutionStore(store)
    coordinator = CleanupCoordinator(
        inventory=LifecycleInventory(inventory),
        store=execution_store,
        lifecycle=lifecycle,
        actor=audit_actor,
    )

    @app.get("/admin/api/lifecycle-capabilities")
    async def lifecycle_capabilities():
        return {
            "enabled": lifecycle.enabled,
            "build": lifecycle.build.enabled,
            "publish": lifecycle.publish.enabled,
            "runner": lifecycle.runner.enabled,
            "consoleSchemaAutoInit": True,
        }

    @app.get("/admin/api/environments/{environment_id}/lifecycle-plan")
    async def lifecycle_plan(environment_id: str):
        if not lifecycle.enabled:
            raise HTTPException(
                503,
                "Configure Build, Publish and Runner owner-service lifecycle endpoints",
            )

        try:
            return await asyncio.to_thread(coordinator.plan, environment_id)
        except LifecycleUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except LifecycleRejected as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/admin/api/cleanup-requests/{request_id}/execute")
    async def execute_cleanup(request_id: str):
        if not lifecycle.enabled:
            raise HTTPException(
                503,
                "Owner-service lifecycle APIs are not fully configured",
            )

        try:
            return await asyncio.to_thread(coordinator.execute, request_id)
        except LifecycleUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except LifecycleRejected as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/admin/api/cleanup-executions")
    async def cleanup_executions(limit: int = 100):
        try:
            return {
                "executions": await asyncio.to_thread(
                    execution_store.list_recent,
                    limit,
                )
            }
        except Exception as exc:
            raise HTTPException(
                503,
                f"cleanup execution store unavailable: {type(exc).__name__}",
            ) from exc
