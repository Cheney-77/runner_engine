from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse

from .auth.dependency import get_current_user
from .lifecycle_routes import install_publish_lifecycle_routes
from .progress_routes import install_publish_progress_routes
from .lifecycle_service import LifecyclePublishService
from .model import AnalyzeRequest, BackendRequest, CreateVirtualContractRequest, MinioFolderDownloadRequest
from .service import EdgeDependencyConflictError, PublishError, PublishService, PublishSettings
from .utils.minio_tools import (
    MinioAccessDeniedError,
    MinioBucketNotFoundError,
    MinioConfigurationError,
    MinioFolderNotFoundError,
    MinioToolError,
    download_minio_folder,
)


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


def _service_from_env() -> LifecyclePublishService:
    workspace_root = os.environ.get("PUBLISH_WORKSPACE_ROOT", "")
    catalog_root = os.environ.get("PUBLISH_CATALOG_ROOT", "")
    source_root = os.environ.get("PUBLISH_SOURCE_ROOT", "")
    native_artifact_root = os.environ.get("PUBLISH_NATIVE_ARTIFACT_ROOT", "")
    edge_bundle_root = os.environ.get("PUBLISH_EDGE_BUNDLE_ROOT", "")
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
    native_root = (
        Path(native_artifact_root)
        if native_artifact_root
        else catalog_path / "_nifi_native_packages"
    )

    return LifecyclePublishService(
        PublishSettings(
            workspace_root=Path(workspace_root),
            catalog_root=catalog_path,
            source_root=(
                Path(source_root)
                if source_root
                else catalog_path / "_publish_sources"
            ),
            native_artifact_root=native_root,
            edge_bundle_root=(
                Path(edge_bundle_root)
                if edge_bundle_root
                else native_root / "_edge_bundles"
            ),
            database_url=database_url,
            build_service_url=build_service_url,
            default_profile=os.environ.get(
                "PUBLISH_DEFAULT_PROFILE",
                "standard",
            ),
            # default_user_id=os.environ.get("PUBLISH_DEFAULT_USER_ID", "1"),
            edge_python_version=os.environ.get(
                "PUBLISH_EDGE_PYTHON_VERSION",
                "3.12",
            ),
            edge_uv_default_index=(
                    os.environ.get("PUBLISH_EDGE_UV_DEFAULT_INDEX")
                    or None
            ),
            edge_require_binary=(
                    os.environ.get(
                        "PUBLISH_EDGE_REQUIRE_BINARY",
                        "true",
                    ).lower()
                    not in {"0", "false", "no"}
            ),
            build_timeout_seconds=int(
                os.environ.get(
                    "PUBLISH_BUILD_TIMEOUT_SECONDS",
                    "1800",
                )
            ),
            publish_worker_threads=int(
                    os.environ.get(
                            "PUBLISH_JOB_WORKERS",
                            "4",
                   )
            ),
            max_source_bytes=int(
                os.environ.get(
                    "PUBLISH_MAX_SOURCE_BYTES",
                    str(500 * 1024 * 1024),
                )
            ),
            auth_mode=os.environ.get("PUBLISH_AUTH_MODE", "required").strip().lower(),
            demo_user_id=os.environ.get("PUBLISH_DEMO_USER_ID") or None,
            jwt_secret=os.environ.get("PUBLISH_JWT_SECRET") or None,
        )
    )

def _user_id(authenticated_user: Any) -> str:
    """Only read the ID returned by the validated authentication dependency."""
    raw = (authenticated_user.get("user_id") if isinstance(authenticated_user, dict)
           else getattr(authenticated_user, "user_id", None))
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise HTTPException(status_code=401, detail="Authenticated user_id is missing")
    value = str(raw)
    if not value or value.strip() != value or any(char in value for char in ("/", "\\", "\x00")):
        raise HTTPException(status_code=401, detail="Authenticated user_id is invalid")
    return value

_configure_logging(os.environ.get("PUBLISH_LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def create_app(service: PublishService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active_service = service or _service_from_env()
        app.state.publish_service = active_service
        logger.info("Publish Service started lifecycle=%s", type(active_service).__name__)
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
            "edgeNativePublishing": True,
            "runtimeLifecycle": True,
            "adminLifecycleConfigured": bool(os.environ.get("PUBLISH_ADMIN_TOKEN", "").strip()),
        }

    @application.get("/v1/edge/platforms")
    def edge_platforms(request: Request, user: Any = Depends(get_current_user)) -> dict:
        _user_id(user)
        return request.app.state.publish_service.edge_platforms()

    @application.get("/v1/edge/deployments")
    def edge_deployments(request: Request, user: Any = Depends(get_current_user)) -> dict:
        return request.app.state.publish_service.edge_deployments(user_id=_user_id(user))

    @application.get("/v1/edge/bundles")
    def edge_bundles(request: Request, limit: int = 50, user: Any = Depends(get_current_user)) -> dict:
        return request.app.state.publish_service.edge_bundles(user_id=_user_id(user), limit=limit)

    @application.get("/v1/edge/operations")
    def edge_operations(request: Request, limit: int = 100, user: Any = Depends(get_current_user)) -> dict:
        return request.app.state.publish_service.edge_operations(user_id=_user_id(user), limit=limit)

    @application.get("/v1/edge/bundles/{bundle_id}/download")
    def download_edge_bundle(bundle_id: str, request: Request, user: Any = Depends(get_current_user)):
        try:
            path = request.app.state.publish_service.edge_bundle_file(bundle_id, user_id=_user_id(user))
        except PublishError as exc:
            raise HTTPException(status_code=404, detail="edge bundle not found") from exc
        return FileResponse(path, media_type="application/zip", filename=path.name)

    @application.post("/v1/authoring/analyze")
    def analyze(payload: AnalyzeRequest, request: Request, user: Any = Depends(get_current_user)) -> dict:
        _user_id(user)
        try:
            return request.app.state.publish_service.analyze(
                payload.workspace,
                python_root=payload.python_root,
            )
        except (ValueError, PublishError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post("/v1/operators")
    def create_operator(
        payload: CreateVirtualContractRequest,
        request: Request,
        user: Any = Depends(get_current_user),
    ) -> dict:
        try:
            return request.app.state.publish_service.create_virtual_contract(payload, user_id=_user_id(user))
        except (ValueError, PublishError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            logger.exception("Creating virtual operator contract failed")
            raise

    @application.get("/v1/operators/{operator_id}")
    def get_operator(operator_id: str, request: Request, user: Any = Depends(get_current_user)) -> dict:
        try:
            return request.app.state.publish_service.get_operator(operator_id, user_id=_user_id(user))
        except PublishError as exc:
            raise HTTPException(status_code=404, detail="operator not found") from exc

    @application.post("/v1/operators/{operator_id}/backends/{backend}/compile")
    def compile_backend(
        operator_id: str,
        backend: str,
        payload: BackendRequest,
        request: Request,
        user: Any = Depends(get_current_user),
    ) -> dict:
        try:
            return request.app.state.publish_service.compile_backend(
                operator_id, backend, payload.options, user_id=_user_id(user),
            )
        except EdgeDependencyConflictError as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        except (ValueError, PublishError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            logger.exception("Backend contract compilation failed")
            raise

    @application.post("/v1/operators/{operator_id}/backends/{backend}/publish", status_code=202,)
    def publish_backend(
        operator_id: str,
        backend: str,
        payload: BackendRequest,
        request: Request,
        user: Any = Depends(get_current_user),
    ) -> dict:
        try:
            return request.app.state.publish_service.publish_backend(
                operator_id, backend, payload.options, user_id=_user_id(user),
            )
        except EdgeDependencyConflictError as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        except (ValueError, PublishError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            logger.exception("Backend publish failed")
            raise

    @application.post("/v1/minio/download-folder")
    def download_minio_folder_route(
        payload: MinioFolderDownloadRequest,
        request: Request,
        user: Any = Depends(get_current_user),
    ) -> dict:
        _user_id(user)
        workspace_root = request.app.state.publish_service.settings.workspace_root
        try:
            result = download_minio_folder(
                bucket=payload.bucket,
                folder_path=payload.folder_path,
                workspace_root=Path(workspace_root),
            )

            logger.info(
                "MinIO folder downloaded "
                "bucket=%s prefix=%s workspace=%s "
                "files=%s bytes=%s",
                result.bucket,
                result.folder_path,
                result.local_path,
                result.downloaded_files,
                result.downloaded_bytes,
            )

            return {
                "ok": True,
                "data": result.to_dict(),
            }

        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        except MinioBucketNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        except MinioFolderNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        except MinioAccessDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        except MinioConfigurationError as exc:
            logger.exception("MinIO configuration error")

            raise HTTPException(status_code=500, detail=str(exc)) from exc

        except MinioToolError as exc:
            logger.exception("MinIO folder download failed")

            raise HTTPException(status_code=502, detail=str(exc)) from exc

        except Exception:
            logger.exception("Unexpected MinIO folder download failure")
            raise

    @application.get("/v1/operators")
    def get_operator_list(run_type: str, request: Request, user: Any = Depends(get_current_user)):
        try:
            return request.app.state.publish_service.get_operator_list(run_type, _user_id(user))
        except PublishError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Service-level admin token is intentionally independent of the user JWT.
    install_publish_progress_routes(application)
    install_publish_lifecycle_routes(application)
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
