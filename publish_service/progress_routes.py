from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from .auth.dependency import get_current_user, get_publish_service
from .service import PublishError, PublishService


def _user_id(user: Any) -> str:
    if isinstance(user, dict):
        value = user.get("user_id")
    else:
        value = getattr(user, "user_id", None)

    if isinstance(value, bool) or value is None or not str(value).strip():
        raise HTTPException(
            status_code=401,
            detail="Authenticated user is missing user_id",
        )
    return str(value)


def _sse(event: str, data: dict[str, Any], *, event_id: str | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    for line in payload.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def install_publish_progress_routes(application: FastAPI) -> None:
    @application.get("/v1/publish-jobs/{job_id}")
    def get_publish_job(
        job_id: str,
        service: PublishService = Depends(get_publish_service),
        user: Any = Depends(get_current_user),
    ) -> dict:
        try:
            return service.get_publish_job(
                job_id,
                user_id=_user_id(user),
            )
        except PublishError as exc:
            # 404 intentionally hides whether another user's job exists.
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get("/v1/publish-jobs/{job_id}/events")
    async def publish_job_events(
        job_id: str,
        request: Request,
        service: PublishService = Depends(get_publish_service),
        user: Any = Depends(get_current_user),
    ):
        # Capture the authenticated identity once for this stream. Never read
        # user_id from query/body and never fall back to a default user.
        user_id = _user_id(user)

        try:
            await asyncio.to_thread(
                service.get_publish_job,
                job_id,
                user_id=user_id,
            )
        except PublishError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        raw_last_id = request.headers.get("last-event-id", "").strip()
        try:
            last_seq = max(0, int(raw_last_id)) if raw_last_id else 0
        except ValueError:
            last_seq = 0

        async def stream():
            nonlocal last_seq
            last_keepalive = time.monotonic()

            while True:
                if await request.is_disconnected():
                    break

                try:
                    # PublishStore is synchronous; do not block FastAPI's event
                    # loop with a PostgreSQL call every polling interval.
                    job = await asyncio.to_thread(
                        service.get_publish_job,
                        job_id,
                        user_id=user_id,
                    )
                except PublishError:
                    yield _sse(
                        "failed",
                        {
                            "jobId": job_id,
                            "status": "FAILED",
                            "error": "publish job is no longer available",
                        },
                    )
                    break

                snapshot = job.get("progress")
                if isinstance(snapshot, dict):
                    seq = int(snapshot.get("seq") or 0)
                    if seq > last_seq:
                        yield _sse("progress", snapshot, event_id=str(seq))
                        last_seq = seq

                status = job.get("status")
                if status == "READY":
                    yield _sse("completed", job)
                    break
                if status == "FAILED":
                    yield _sse("failed", job)
                    break

                now = time.monotonic()
                if now - last_keepalive >= 15:
                    yield ": keep-alive\n\n"
                    last_keepalive = now

                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
