from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from .model import AnalyzeRequest, BackendRequest, CreateVirtualContractRequest
from .service import PublishError, PublishService, PublishSettings


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(handler)


def _service_from_env() -> PublishService:
    workspace_root = os.environ.get("PUBLISH_WORKSPACE_ROOT", "")
    catalog_root = os.environ.get("PUBLISH_CATALOG_ROOT", "")
    source_root = os.environ.get("PUBLISH_SOURCE_ROOT", "")
    native_artifact_root = os.environ.get("PUBLISH_NATIVE_ARTIFACT_ROOT", "")
    database_url = os.environ.get("PUBLISH_DATABASE_URL", "")
    build_service_url = os.environ.get("PUBLISH_BUILD_SERVICE_URL", "http://127.0.0.1:9088")

    required = {
        "PUBLISH_WORKSPACE_ROOT": workspace_root,
        "PUBLISH_CATALOG_ROOT": catalog_root,
        "PUBLISH_DATABASE_URL": database_url,
    }
    for name, value in required.items():
        if not value:
            raise RuntimeError(f"{name} is required")

    catalog_path = Path(catalog_root)
    return PublishService(PublishSettings(
        workspace_root=Path(workspace_root),
        catalog_root=catalog_path,
        source_root=Path(source_root) if source_root else catalog_path / "_publish_sources",
        native_artifact_root=(
            Path(native_artifact_root)
            if native_artifact_root
            else catalog_path / "_nifi_native_packages"
        ),
        database_url=database_url,
        build_service_url=build_service_url,
        default_profile=os.environ.get("PUBLISH_DEFAULT_PROFILE", "standard"),
        build_timeout_seconds=int(os.environ.get("PUBLISH_BUILD_TIMEOUT_SECONDS", "1800")),
        max_source_bytes=int(os.environ.get("PUBLISH_MAX_SOURCE_BYTES", str(20 * 1024 * 1024))),
    ))


_configure_logging(os.environ.get("PUBLISH_LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def create_app(service: PublishService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active_service = service or _service_from_env()
        app.state.publish_service = active_service
        logger.info("Publish Service started")
        try:
            yield
        finally:
            logger.info("Publish Service stopping")
            if service is None:
                active_service.close()

    application = FastAPI(
        title="Managed Python Operator Publish Service",
        version="3.3.0",
        lifespan=lifespan,
    )

    @application.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "version": "3.3.0",
            "api": "virtual-contract-v1",
            "backends": ["runner", "nifi_native"],
            "compiledPlanArtifact": False,
        }

    @application.post("/v1/authoring/analyze")
    def analyze(payload: AnalyzeRequest, request: Request) -> dict:
        try:
            return request.app.state.publish_service.analyze(
                payload.workspace,
                python_root=payload.python_root,
            )
        except (ValueError, PublishError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post("/v1/operators")
    def create_operator(payload: CreateVirtualContractRequest, request: Request) -> dict:
        try:
            return request.app.state.publish_service.create_virtual_contract(payload)
        except (ValueError, PublishError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            logger.exception("Creating virtual operator contract failed")
            raise

    @application.get("/v1/operators/{operator_id}")
    def get_operator(operator_id: str, request: Request) -> dict:
        try:
            return request.app.state.publish_service.get_operator(operator_id)
        except PublishError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post("/v1/operators/{operator_id}/backends/{backend}/compile")
    def compile_backend(
        operator_id: str,
        backend: str,
        payload: BackendRequest,
        request: Request,
    ) -> dict:
        try:
            return request.app.state.publish_service.compile_backend(
                operator_id,
                backend,
                payload.options,
            )
        except (ValueError, PublishError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            logger.exception("Backend contract compilation failed")
            raise

    @application.post("/v1/operators/{operator_id}/backends/{backend}/publish")
    def publish_backend(
        operator_id: str,
        backend: str,
        payload: BackendRequest,
        request: Request,
    ) -> dict:
        try:
            return request.app.state.publish_service.publish_backend(
                operator_id,
                backend,
                payload.options,
            )
        except (ValueError, PublishError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            logger.exception("Backend publish failed")
            raise

    return application


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Managed Python Operator Publish Service v3.3")
    parser.add_argument("--listen-host", default=os.environ.get("PUBLISH_LISTEN_HOST", "127.0.0.1"))
    parser.add_argument("--listen-port", type=int, default=int(os.environ.get("PUBLISH_LISTEN_PORT", "9090")))
    parser.add_argument("--log-level", default=os.environ.get("PUBLISH_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    _configure_logging(args.log_level)
    uvicorn.run(app, host=args.listen_host, port=args.listen_port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
