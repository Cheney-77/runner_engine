from __future__ import annotations

import asyncio

from fastapi import FastAPI

from .runner_runtime import RunnerRuntimeReader


def install_runner_runtime_routes(
    app: FastAPI,
    *,
    reader: RunnerRuntimeReader | None = None,
) -> None:
    active_reader = reader or RunnerRuntimeReader.environment()

    @app.get("/observe/api/runner-runtime")
    async def runner_runtime():
        return await asyncio.to_thread(active_reader.report)
