"""Exact-DDL observability ASGI layer; original observe/ files untouched.

This module deliberately DOES NOT import observe.main because its historical
module-level app reads legacy OBS_TABLE_MAP_FILE at import time. Only the
unified entry point uses this new explicit-PostgreSQL adapter.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from observe.runner_runtime import RunnerRuntimeReader
from observe.sandbox import SandboxReader
from observe.settings import Settings

from .metrics import exact_readers

OLD_STATIC = Path(__file__).resolve().parents[1] / "observe" / "static"


def make_app(
    settings: Settings,
    *,
    database_readers=None,
    sandbox_reader=None,
    runner_runtime_reader=None,
):
    app = FastAPI(
        title="Managed Python Exact PostgreSQL Observability",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.readers = database_readers or exact_readers(
        settings.db_urls,
        schemas=settings.db_schemas,
    )
    app.state.sandbox = sandbox_reader or SandboxReader(settings)
    app.state.runner_runtime = (
        runner_runtime_reader or RunnerRuntimeReader.environment()
    )

    @app.middleware("http")
    async def protect(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/observe/health")
    async def health():
        return {
            "ok": True,
            "service": "exact-postgresql-observability",
        }

    @app.get("/observe/api/databases")
    async def database_api():
        names = ("publish", "build", "runner")
        reports = await asyncio.gather(
            *(
                asyncio.to_thread(app.state.readers[name].report)
                for name in names
            )
        )
        return {
            "sampledAt": datetime.now(timezone.utc).isoformat(),
            "services": dict(zip(names, reports)),
        }

    @app.get("/observe/api/sandboxes")
    async def sandbox_api():
        return await app.state.sandbox.report()

    @app.get("/observe/api/runner-runtime")
    async def runner_runtime_api():
        return await asyncio.to_thread(app.state.runner_runtime.report)

    @app.get("/observe/api/overview")
    async def overview():
        databases, sandbox, runner_runtime = await asyncio.gather(
            database_api(),
            sandbox_api(),
            runner_runtime_api(),
        )
        return {
            "sampledAt": datetime.now(timezone.utc).isoformat(),
            "databases": databases["services"],
            "sandboxes": sandbox,
            "runnerRuntime": runner_runtime,
        }

    app.mount(
        "/observe/static",
        StaticFiles(directory=OLD_STATIC),
        name="observe-assets",
    )
    return app
