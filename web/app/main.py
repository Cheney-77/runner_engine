"""v3.3 BFF: only the endpoints in the user's Publish Service source."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .gateway import NO_BODY, PublishGateway, segment

STATIC = Path(__file__).resolve().parent / "static"
app = FastAPI(
    title="Managed Python v3.3 Operator Authoring Web",
    version="2.0.0",
)
app.state.gateway = PublishGateway()


async def get_json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Expected a JSON object")
    return body


def path_id(value: str, name: str) -> str:
    try:
        return segment(value, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def forward(
    method: str,
    path: str,
    *,
    body: Any = NO_BODY,
    long_running: bool = False,
) -> Response:
    try:
        remote = await app.state.gateway.request(
            method, path, body=body, long_running=long_running
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

    # Keep upstream status and response unchanged for FastAPI errors (400,
    # 404, 409, 422), and for other upstream success/failure responses.
    content_type = remote.headers.get("content-type")
    headers = {"content-type": content_type} if content_type else {}
    return Response(
        content=remote.content,
        status_code=remote.status_code,
        headers=headers,
    )


@app.get("/api/health")
async def health():
    return await forward("GET", "/health")


@app.get("/api/openapi")
async def openapi():
    # This route is introspection only, not a new Publish Service API.
    return await forward("GET", "/openapi.json")


@app.post("/api/authoring/analyze")
async def analyze(request: Request):
    return await forward(
        "POST", "/v1/authoring/analyze",
        body=await get_json_object(request),
    )


@app.post("/api/operators")
async def create_operator(request: Request):
    return await forward(
        "POST", "/v1/operators",
        body=await get_json_object(request),
        long_running=True,
    )


@app.get("/api/operators/{operator_id}")
async def get_operator(operator_id: str):
    return await forward(
        "GET", f"/v1/operators/{path_id(operator_id, 'operator_id')}",
    )


@app.post("/api/operators/{operator_id}/backends/{backend}/compile")
async def compile_backend(operator_id: str, backend: str, request: Request):
    return await forward(
        "POST",
        (
            f"/v1/operators/{path_id(operator_id, 'operator_id')}"
            f"/backends/{path_id(backend, 'backend')}/compile"
        ),
        body=await get_json_object(request),
        long_running=True,
    )


@app.post("/api/operators/{operator_id}/backends/{backend}/publish")
async def publish_backend(operator_id: str, backend: str, request: Request):
    return await forward(
        "POST",
        (
            f"/v1/operators/{path_id(operator_id, 'operator_id')}"
            f"/backends/{path_id(backend, 'backend')}/publish"
        ),
        body=await get_json_object(request),
        long_running=True,
    )


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Publish-only v3.3 Web Wizard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9095)
    options = parser.parse_args()
    uvicorn.run("app.main:app", host=options.host, port=options.port)


if __name__ == "__main__":
    main()
