"""One ASGI process, three independent modules, unchanged Publish Web code.

Path routing deliberately preserves the ORIGINAL app's path and root.
  /                 => app.main.app (untouched)
  /api/*, /static/*  => app.main.app (untouched)
  /observe/*         => existing observe application with EXACT DDL readers
  /admin/*           => operations manager (no web login; network-restricted deployment)
  /console/*         => small new module landing page
"""
from __future__ import annotations

import argparse
from pathlib import Path

import logging

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.main import app as publish_app
from admin.inventory import Inventory
from admin.main import make_app as make_admin_app
from admin.store import AdminStore
from admin.retry_client import RetryClient
from .observe_app import make_app as make_observe_app

from .metrics import exact_readers
from .settings import ConsoleSettings

STATIC = Path(__file__).resolve().parent / "static"


class Unified:
    def __init__(self, settings: ConsoleSettings | None = None,
                 *, publishing=None, observation=None, administration=None):
        self.settings = settings or ConsoleSettings.environment()
        self.publish = publishing or publish_app
        self.observe = observation or make_observe_app(
            settings=self.settings.observation,
            database_readers=exact_readers(
                self.settings.observation.db_urls,
                schemas=self.settings.observation.db_schemas,
            ),
        )
        self.admin = administration or make_admin_app(
            Inventory(
                self.settings.observation.db_urls,
                schemas=self.settings.observation.db_schemas,
            ),
            AdminStore(self.settings.admin_db_url),
            audit_actor=self.settings.audit_actor,
            public_origin=self.settings.public_origin,
            retry_client=RetryClient(
                self.settings.build_service_url,
                self.settings.build_bearer_token,
            ),
        )
        hub = FastAPI(
            title="Managed Python Unified Console",
            docs_url=None, redoc_url=None, openapi_url=None,
        )

        @hub.get("/console")
        async def slash():
            return RedirectResponse("/console/", status_code=307)

        @hub.get("/console/")
        async def index():
            return FileResponse(STATIC / "index.html")

        @hub.get("/observe")
        async def observation_slash():
            return RedirectResponse("/observe/", status_code=307)

        @hub.get("/observe/")
        async def observation_index():
            # No web login. Legacy observe/static files remain unchanged.
            return FileResponse(
                STATIC / "observe.html",
                headers={"Cache-Control": "no-store"},
            )

        @hub.get("/admin")
        async def admin_slash():
            return RedirectResponse("/admin/", status_code=307)

        hub.mount("/console/static", StaticFiles(directory=STATIC), name="console-static")
        self.hub = hub

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            # The existing apps currently have no additional lifespan
            # resources; retain the original Publish Web lifecycle unchanged.
            await self.publish(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in ("/observe", "/observe/"):
            await self.hub(scope, receive, send)
        elif path.startswith("/observe/"):
            await self.observe(scope, receive, send)
        elif path == "/admin":
            await self.hub(scope, receive, send)
        elif path.startswith("/admin/"):
            await self.admin(scope, receive, send)
        elif path == "/console" or path.startswith("/console/"):
            await self.hub(scope, receive, send)
        else:
            await self.publish(scope, receive, send)


app = Unified()


def main():
    parser = argparse.ArgumentParser(
        description="Managed Python unified Publish + Observability + Operations"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9095)
    args = parser.parse_args()
    if args.host not in ("localhost", "127.0.0.1", "::1"):
        config = app.settings
        if not config.allow_remote_no_auth:
            parser.error(
                "Unauthenticated non-loopback access requires explicit "
                "CONSOLE_ALLOW_REMOTE_NO_AUTH=1 and network-level access controls."
            )
        if not config.public_origin:
            parser.error(
                "Set CONSOLE_PUBLIC_ORIGIN to the actual browser origin "
                "for same-origin protection of management writes."
            )
        logging.warning(
            "Web login is disabled. Restrict access to the console at the "
            "reverse proxy/firewall. Do not expose admin endpoints publicly."
        )
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
