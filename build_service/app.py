from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from .service import BuildService, BuildSettings
from .store import BuildStore


class ResolveRequest(BaseModel):
    requirements: list[str]


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        root.addHandler(handler)

    build_logger = logging.getLogger("build_service")
    build_logger.setLevel(level)
    build_logger.propagate = True


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
    }


def _service_from_env() -> BuildService:
    database_url = os.environ.get("BUILD_DATABASE_URL", "")
    base_image = os.environ.get("BUILD_RUNNER_BASE_IMAGE", "")
    registry_repo = os.environ.get("BUILD_REGISTRY_REPO", "")

    required = {
        "BUILD_DATABASE_URL": database_url,
        "BUILD_RUNNER_BASE_IMAGE": base_image,
        "BUILD_REGISTRY_REPO": registry_repo,
    }
    for name, value in required.items():
        if not value:
            raise RuntimeError(f"{name} is required")

    store = BuildStore(database_url)
    settings = BuildSettings(
        base_image=base_image,
        registry_repo=registry_repo,
        python_version=os.environ.get("BUILD_PYTHON_VERSION", "3.12"),
        platform=os.environ.get("BUILD_PLATFORM", "linux/amd64"),
        uv_python_platform=os.environ.get("BUILD_UV_PYTHON_PLATFORM", "x86_64-unknown-linux-gnu"),
        build_policy_version=os.environ.get("BUILD_POLICY_VERSION", "1"),
        uv_default_index=os.environ.get("UV_DEFAULT_INDEX"),
        allow_superset_reuse=_as_bool(os.environ.get("BUILD_ALLOW_SUPERSET_REUSE", "true")),
    )
    return BuildService(store, settings)


_configure_logging(os.environ.get("BUILD_LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def create_app(service: BuildService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active_service = service or _service_from_env()
        app.state.build_service = active_service
        logger.info("Build Service started")
        try:
            yield
        finally:
            logger.info("Build Service stopping")
            if service is None:
                active_service.store.close()

    application = FastAPI(
        title="Managed Python Build Service",
        version="3.3.0",
        lifespan=lifespan,
    )

    @application.get("/health")
    def health() -> dict:
        return {"ok": True, "version": "3.3.0"}

    @application.post("/v1/runtime-environments/resolve")
    def resolve(payload: ResolveRequest, request: Request) -> dict:
        try:
            row = request.app.state.build_service.resolve(payload.requirements)
            return _serialize_environment(row)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            logger.exception("Runtime environment resolve failed")
            raise

    @application.get("/v1/runtime-environments/{env_key}")
    def get_environment(env_key: str, request: Request) -> dict:
        row = request.app.state.build_service.get(env_key)
        if row is None:
            raise HTTPException(status_code=404, detail="runtime environment not found")
        return _serialize_environment(row)

    @application.post("/v1/runtime-environments/{env_key}/retry")
    def retry_environment(env_key: str, request: Request) -> dict:
        try:
            row = request.app.state.build_service.retry(env_key)
            return _serialize_environment(row)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="runtime environment not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            logger.exception("Runtime environment retry failed env_key=%s", env_key)
            raise

    return application


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Managed Python Build Service v3.3")
    parser.add_argument("--listen-host", default=os.environ.get("BUILD_LISTEN_HOST", "127.0.0.1"))
    parser.add_argument("--listen-port", type=int, default=int(os.environ.get("BUILD_LISTEN_PORT", "9088")))
    parser.add_argument("--log-level", default=os.environ.get("BUILD_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    _configure_logging(args.log_level)

    # Keep Uvicorn's normal server/access logging. Build Service application logs use
    # the dedicated build_service logger configured above.
    uvicorn.run(app, host=args.listen_host, port=args.listen_port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
