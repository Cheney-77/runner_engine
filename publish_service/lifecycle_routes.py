from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field


class RuntimeRetirementRequest(BaseModel):
    runtimeEnvKey: str = Field(min_length=1, max_length=256)
    imageRef: str = Field(min_length=1, max_length=2048)
    reason: str = Field(min_length=5, max_length=1000)


class RuntimeKeyRequest(BaseModel):
    runtimeEnvKey: str = Field(min_length=1, max_length=256)


def _require_admin(request: Request) -> None:
    expected = os.environ.get("PUBLISH_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="PUBLISH_ADMIN_TOKEN is not configured",
        )

    authorization = request.headers.get("Authorization", "")
    if (
        not authorization.startswith("Bearer ")
        or not hmac.compare_digest(authorization[7:], expected)
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


def install_publish_lifecycle_routes(application: FastAPI) -> None:
    @application.post("/v1/admin/runner-runtimes/retire")
    def retire_runner_runtime(
        payload: RuntimeRetirementRequest,
        request: Request,
    ) -> dict:
        _require_admin(request)
        try:
            return (
                request.app.state.publish_service.begin_runner_runtime_retirement(
                    runtime_env_key=payload.runtimeEnvKey,
                    image_ref=payload.imageRef,
                    reason=payload.reason,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post("/v1/admin/runner-runtimes/cancel-retirement")
    def cancel_runner_runtime_retirement(
        payload: RuntimeKeyRequest,
        request: Request,
    ) -> dict:
        _require_admin(request)
        try:
            return (
                request.app.state.publish_service.cancel_runner_runtime_retirement(
                    runtime_env_key=payload.runtimeEnvKey
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post("/v1/admin/runner-runtimes/finalize-retirement")
    def finalize_runner_runtime_retirement(
        payload: RuntimeKeyRequest,
        request: Request,
    ) -> dict:
        _require_admin(request)
        try:
            return (
                request.app.state.publish_service.finalize_runner_runtime_retirement(
                    runtime_env_key=payload.runtimeEnvKey
                )
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="runtime lifecycle gate not found",
            ) from exc
