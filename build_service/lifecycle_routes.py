from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field


class RetirementRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


class DeleteArtifactRequest(BaseModel):
    expectedImageRef: str = Field(min_length=1, max_length=2048)


def _require_admin(request: Request) -> None:
    expected = os.environ.get("BUILD_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="BUILD_ADMIN_TOKEN is not configured",
        )

    authorization = request.headers.get("Authorization", "")
    if (
        not authorization.startswith("Bearer ")
        or not hmac.compare_digest(authorization[7:], expected)
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


def install_build_lifecycle_routes(application: FastAPI) -> None:
    @application.get(
        "/v1/admin/runtime-environments/{environment_id}/cleanup-plan"
    )
    def cleanup_plan(environment_id: str, request: Request) -> dict:
        _require_admin(request)
        try:
            return request.app.state.build_service.cleanup_plan(environment_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="runtime environment not found",
            ) from exc

    @application.post(
        "/v1/admin/runtime-environments/{environment_id}/retire"
    )
    def retire_environment(
        environment_id: str,
        payload: RetirementRequest,
        request: Request,
    ) -> dict:
        _require_admin(request)
        try:
            return request.app.state.build_service.retire_environment(
                environment_id,
                reason=payload.reason,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="runtime environment not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post(
        "/v1/admin/runtime-environments/{environment_id}/cancel-retirement"
    )
    def cancel_retirement(environment_id: str, request: Request) -> dict:
        _require_admin(request)
        try:
            return request.app.state.build_service.cancel_environment_retirement(
                environment_id
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="runtime environment not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.delete(
        "/v1/admin/runtime-environments/{environment_id}/artifact"
    )
    def delete_artifact(
        environment_id: str,
        payload: DeleteArtifactRequest,
        request: Request,
    ) -> dict:
        _require_admin(request)
        try:
            return request.app.state.build_service.delete_environment_artifact(
                environment_id,
                expected_image_ref=payload.expectedImageRef,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="runtime environment not found",
            ) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
