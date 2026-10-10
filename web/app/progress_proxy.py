from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .gateway import segment


def install_publish_progress_proxy(application: FastAPI) -> None:
    @application.get("/api/publish-jobs/{job_id}")
    async def publish_job_status(job_id: str):
        try:
            remote = await application.state.gateway.request(
                "GET",
                f"/v1/publish-jobs/{segment(job_id, 'job_id')}",
            )
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=504,
                detail=f"Publish Service timeout: {exc}",
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Publish Service unreachable: {exc}",
            ) from exc

        content_type = remote.headers.get(
            "content-type",
            "application/json",
        )
        return Response(
            content=remote.content,
            status_code=remote.status_code,
            headers={"content-type": content_type},
        )

    @application.get("/api/publish-jobs/{job_id}/events")
    async def publish_job_events(job_id: str, request: Request):
        path = (
            f"/v1/publish-jobs/{segment(job_id, 'job_id')}/events"
        )
        stream_context = application.state.gateway.stream(
            "GET",
            path,
            accept="text/event-stream",
        )

        try:
            remote = await stream_context.__aenter__()
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=504,
                detail=f"Publish Service timeout: {exc}",
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Publish Service unreachable: {exc}",
            ) from exc

        if remote.status_code >= 400:
            content = await remote.aread()
            status = remote.status_code
            content_type = remote.headers.get(
                "content-type",
                "application/json",
            )
            await stream_context.__aexit__(None, None, None)
            return Response(
                content=content,
                status_code=status,
                headers={"content-type": content_type},
            )

        async def body():
            try:
                async for chunk in remote.aiter_raw():
                    if await request.is_disconnected():
                        break
                    if chunk:
                        yield chunk
            finally:
                await stream_context.__aexit__(None, None, None)

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )
