"""Standalone admin surface, mounted by the unified console without changes to app/.

Admin owns only console_ops metadata. Destructive resource lifecycle actions
are coordinated through Build/Publish/Runner owner-service APIs.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .inventory import Inventory
from .lifecycle_client import LifecycleClient
from .lifecycle_inventory import LifecycleInventory
from .lifecycle_routes import install_lifecycle_routes
from .retry_client import RetryClient, RetryRejected, RetryUnavailable
from .store import AdminStore, Conflict, StorageUnavailable

STATIC = Path(__file__).resolve().parent / "static"


class NoteCreate(BaseModel):
    assetType: str
    assetId: str
    note: str = Field(min_length=1, max_length=1000)


class NoteUpdate(BaseModel):
    note: str = Field(min_length=1, max_length=1000)
    version: int = Field(ge=1)


class NoteDelete(BaseModel):
    version: int = Field(ge=1)


class CleanupDraft(BaseModel):
    environmentId: str
    reason: str = Field(min_length=10, max_length=1000)
    confirmation: str


def make_app(
    inventory: Inventory,
    store: AdminStore,
    *,
    audit_actor: str = "anonymous-console",
    public_origin: str = "",
    retry_client: RetryClient | None = None,
) -> FastAPI:
    retry_client = retry_client or RetryClient()
    lifecycle_client = LifecycleClient.environment()

    # Use one lifecycle-aware preview for both the ordinary Admin page and the
    # execute path. This removes the old historical publish_jobs/result_json
    # blocker from the page-level preflight and replaces it with CURRENT
    # PUBLISHED Runner variant checks.
    lifecycle_inventory = LifecycleInventory(inventory)

    app = FastAPI(
        title="Managed Python Operations Manager",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.inventory = inventory

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            expected = public_origin or (
                f"{request.url.scheme}://{request.headers.get('host', '')}"
            )
            actual = request.headers.get("origin", "")
            if (
                actual != expected
                or request.headers.get("x-admin-request") != "1"
                or request.headers.get("content-type", "").split(";")[0].lower()
                != "application/json"
            ):
                return JSONResponse(
                    {
                        "detail": (
                            "Administrative action requires same-origin JSON request."
                        )
                    },
                    status_code=403,
                )

        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
        )
        return response

    def safe_store(fn, *args):
        try:
            return fn(*args)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except StorageUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/admin/")
    async def homepage():
        return FileResponse(STATIC / "index.html")

    @app.get("/admin/api/overview")
    async def overview():
        inventory_result, metadata = await asyncio.gather(
            asyncio.to_thread(inventory.report),
            asyncio.to_thread(lambda: safe_store(store.overview)),
            return_exceptions=True,
        )
        if isinstance(inventory_result, Exception):
            inventory_result = {
                "error": (
                    "Inventory unavailable; verify read-only database connections."
                )
            }
        if isinstance(metadata, Exception):
            metadata = {
                "status": "error",
                "notes": [],
                "requests": [],
                "audit": [],
                "message": (
                    "Admin metadata DB unavailable; check ADMIN_DB_URL and "
                    "console_ops grants."
                ),
            }

        return {
            "inventory": inventory_result,
            "management": metadata,
            "capabilities": {
                "coreDatabaseWrites": False,
                "physicalImageDeletion": lifecycle_client.enabled,
                "annotationsCrud": metadata.get("status") == "ok",
                "cleanupDrafts": metadata.get("status") == "ok",
                "cleanupExecute": (
                    lifecycle_client.enabled
                    and metadata.get("status") == "ok"
                ),
                "buildRetry": (
                    retry_client.enabled
                    and metadata.get("status") == "ok"
                ),
            },
        }

    @app.get("/admin/api/environments/{environment_id}/preview")
    async def preview(environment_id: str):
        try:
            result = await asyncio.to_thread(
                lifecycle_inventory.cleanup_preview,
                environment_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        if result["status"] == "not_found":
            raise HTTPException(404, "Runtime environment not found")
        return result

    @app.post("/admin/api/environments/{env_key}/retry")
    async def retry_failed_environment(env_key: str):
        if not retry_client.enabled:
            raise HTTPException(
                503,
                "Configure ADMIN_BUILD_SERVICE_URL for retry",
            )

        try:
            checked = await asyncio.to_thread(
                inventory.retry_preflight,
                env_key,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

        await asyncio.to_thread(
            lambda: safe_store(
                store.record_action,
                audit_actor,
                "build.retry.attempt",
                "build_environment",
                checked["id"],
            )
        )

        try:
            result = await retry_client.retry(checked["envKey"])
        except RetryUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except RetryRejected as exc:
            raise HTTPException(
                exc.status if 400 <= exc.status <= 599 else 502,
                str(exc),
            ) from exc

        try:
            await asyncio.to_thread(
                lambda: safe_store(
                    store.record_action,
                    audit_actor,
                    "build.retry.accepted",
                    "build_environment",
                    checked["id"],
                )
            )
            result["auditStatus"] = "recorded"
        except HTTPException:
            result["auditStatus"] = "confirmation_pending"
            result["message"] = (
                "Build accepted retry, but the completion audit could not be saved."
            )

        return result

    @app.post("/admin/api/notes")
    async def add_note(body: NoteCreate, request: Request):
        del request
        try:
            exists = await asyncio.to_thread(
                inventory.asset_exists,
                body.assetType,
                body.assetId,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

        if not exists:
            raise HTTPException(
                404,
                "Asset not found in its owning service",
            )

        return await asyncio.to_thread(
            lambda: safe_store(
                store.create_note,
                audit_actor,
                body.assetType,
                body.assetId,
                body.note,
            )
        )

    @app.patch("/admin/api/notes/{note_id}")
    async def update_note(note_id: str, body: NoteUpdate):
        return await asyncio.to_thread(
            lambda: safe_store(
                store.update_note,
                audit_actor,
                note_id,
                body.note,
                body.version,
            )
        )

    @app.delete("/admin/api/notes/{note_id}")
    async def delete_note(note_id: str, body: NoteDelete):
        return await asyncio.to_thread(
            lambda: safe_store(
                store.delete_note,
                audit_actor,
                note_id,
                body.version,
            )
        )

    @app.post("/admin/api/cleanup-requests")
    async def cleanup_request(body: CleanupDraft):
        try:
            snapshot = await asyncio.to_thread(
                lifecycle_inventory.cleanup_preview,
                body.environmentId,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        if not snapshot["found"]:
            raise HTTPException(
                404 if snapshot["status"] == "not_found" else 503,
                "Environment not available for preflight",
            )

        return await asyncio.to_thread(
            lambda: safe_store(
                store.request_cleanup,
                audit_actor,
                snapshot,
                body.reason,
                body.confirmation,
            )
        )

    @app.post("/admin/api/cleanup-requests/{request_id}/cancel")
    async def cancel(request_id: str):
        return await asyncio.to_thread(
            lambda: safe_store(
                store.cancel_cleanup,
                audit_actor,
                request_id,
            )
        )

    install_lifecycle_routes(
        app,
        inventory=inventory,
        store=store,
        audit_actor=audit_actor,
        lifecycle_client=lifecycle_client,
    )

    app.mount(
        "/admin/static",
        StaticFiles(directory=STATIC),
        name="admin-static",
    )
    return app
