from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


class ProgressResolveRequest(BaseModel):
    requirements: list[str]


def _serialize_environment(row) -> dict:
    if row is None:
        return {"found": False}
    return {
        "found": True,
        "environmentId": str(row["id"]),
        "envKey": row.get("request_env_key", row["env_key"]),
        "runtimeEnvKey": row["env_key"],
        "resolutionKind": row.get("resolution_kind", "exact"),
        "status": row["status"],
        "image": row["image_ref"],
        "error": row["last_error"],
        "canRetry": row["status"] == "FAILED",
        "lifecycleState": row.get("lifecycle_state", "ACTIVE"),
    }


def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return f"event: {event}\ndata: {payload}\n\n"


def install_build_progress_routes(application: FastAPI) -> None:
    @application.post("/v1/runtime-environments/resolve/events")
    async def resolve_with_events(
        payload: ProgressResolveRequest,
        request: Request,
    ):
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        service = request.app.state.build_service

        def report(
            stage: str,
            message: str,
            detail: dict[str, Any] | None = None,
        ) -> None:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                (
                    "progress",
                    {
                        "stage": stage,
                        "message": message,
                        "detail": detail or {},
                    },
                ),
            )

        async def run_resolve() -> None:
            try:
                row = await asyncio.to_thread(
                    service.resolve,
                    payload.requirements,
                    progress=report,
                )
                await queue.put(("result", _serialize_environment(row)))
            except Exception as exc:
                await queue.put(
                    (
                        "error",
                        {
                            "message": f"{type(exc).__name__}: {exc}",
                        },
                    )
                )

        # Keep the task independent from the streaming generator. If the HTTP
        # client disconnects, the build operation is allowed to finish and
        # persist its normal Build Service state.
        task = asyncio.create_task(run_resolve())

        async def stream():
            try:
                while True:
                    try:
                        event_name, data = await asyncio.wait_for(
                            queue.get(),
                            timeout=15.0,
                        )
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue

                    yield _sse(event_name, data)
                    if event_name in {"result", "error"}:
                        break
            finally:
                # Do not cancel a running image build just because the client
                # stopped consuming progress.
                if task.done():
                    try:
                        task.result()
                    except Exception:
                        # run_resolve converts failures into an SSE error event.
                        pass

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
