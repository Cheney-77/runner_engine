"""Independent Observability ASGI app.

Run: python -m observe.main --host 127.0.0.1 --port 9195
Nothing here imports the existing app or registers routes in it.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hmac
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .database import readers
from .runner_runtime import RunnerRuntimeReader
from .sandbox import SandboxReader
from .settings import SERVICES, Settings

STATIC = Path(__file__).resolve().parent / "static"


def authorized(request: Request, settings: Settings) -> bool:
    if not settings.basic_user or not settings.basic_password:
        return True

    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False

    try:
        decoded = base64.b64decode(
            auth[6:],
            validate=True,
        ).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return False

    username, sep, password = decoded.partition(":")
    if not sep:
        return False

    return hmac.compare_digest(
        username,
        settings.basic_user,
    ) and hmac.compare_digest(
        password,
        settings.basic_password,
    )


def make_app(
    settings: Settings | None = None,
    *,
    sandbox_reader: SandboxReader | None = None,
    database_readers: dict | None = None,
    runner_runtime_reader: RunnerRuntimeReader | None = None,
) -> FastAPI:
    settings = settings or Settings.environment()
    if bool(settings.basic_user) != bool(settings.basic_password):
        raise ValueError(
            "OBS_BASIC_USER and OBS_BASIC_PASSWORD must both be set"
        )

    app = FastAPI(
        title="Managed Python Observability",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.database_readers = database_readers or readers(settings)
    app.state.sandbox_reader = sandbox_reader or SandboxReader(settings)
    app.state.runner_runtime_reader = (
        runner_runtime_reader or RunnerRuntimeReader.environment()
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if not authorized(request, settings):
            response = JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Basic realm="Observability"'
                },
            )
        else:
            response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/observe/health")
    async def health():
        return {
            "ok": True,
            "service": "observability",
            "version": "0.1.0",
        }

    @app.get("/observe/api/databases")
    async def databases():
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    app.state.database_readers[key].report
                )
                for key in SERVICES
            )
        )
        return {
            "sampledAt": datetime.now(timezone.utc).isoformat(),
            "services": dict(zip(SERVICES, results)),
        }

    @app.get("/observe/api/sandboxes")
    async def sandboxes():
        return await app.state.sandbox_reader.report()

    @app.get("/observe/api/runner-runtime")
    async def runner_runtime():
        return await asyncio.to_thread(
            app.state.runner_runtime_reader.report
        )

    @app.get("/observe/api/overview")
    async def overview():
        db, sb, runtime = await asyncio.gather(
            databases(),
            sandboxes(),
            runner_runtime(),
        )
        return {
            "sampledAt": datetime.now(timezone.utc).isoformat(),
            "databases": db["services"],
            "sandboxes": sb,
            "runnerRuntime": runtime,
        }

    @app.get("/observe/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/")
    async def root():
        return RedirectResponse("/observe/", status_code=307)

    app.mount(
        "/observe/static",
        StaticFiles(directory=STATIC),
        name="observe-assets",
    )
    return app


app = make_app()


def main():
    parser = argparse.ArgumentParser(
        description="Standalone read-only Managed Python Observability"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9195)
    args = parser.parse_args()

    if args.host not in ("localhost", "127.0.0.1", "::1"):
        config = app.state.settings
        if not (config.basic_user and config.basic_password):
            parser.error(
                "Non-loopback binding requires OBS_BASIC_USER and "
                "OBS_BASIC_PASSWORD. Use TLS and an authenticated reverse proxy "
                "in production."
            )

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
