from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status

from ..service import PublishService, PublishSettings
from .passport import Passport


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    user_uid: str
    is_demo: bool = False


def get_publish_service(request: Request) -> PublishService:
    service = getattr(request.app.state, "publish_service", None)
    if not isinstance(service, PublishService):
        raise HTTPException(status_code=503, detail="Publish Service is not initialized")
    return service


def get_publish_settings(
        service: Annotated[PublishService, Depends(get_publish_service)],
) -> PublishSettings:
    return service.settings


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
        request: Request,
        settings: Annotated[PublishSettings, Depends(get_publish_settings)],
) -> AuthenticatedUser:
    if settings.auth_mode not in {"required", "optional"}:
        raise HTTPException(status_code=503, detail="Invalid PUBLISH_AUTH_MODE")

    authorization = request.headers.get("Authorization")

    if authorization is None:
        if settings.auth_mode == "optional":
            demo_user_id = settings.demo_user_id.strip()
            if not demo_user_id:
                raise HTTPException(status_code=503, detail="Demo user is not configured")
            return AuthenticatedUser(user_id=demo_user_id, user_uid="demo", is_demo=True)
        raise _unauthorized("Missing authorization token")

    scheme, separator, token = authorization.partition(" ")
    if (
            not separator
            or scheme.lower() != "bearer"
            or not token
            or token.strip() != token
            or any(character.isspace() for character in token)
    ):
        raise _unauthorized("Invalid Authorization header")

    if not settings.jwt_secret:
        raise HTTPException(status_code=503, detail="JWT verification is not configured")

    try:
        decoded = Passport.verify(token, secret=settings.jwt_secret)
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("Invalid or expired token") from exc

    user_uid = decoded.get("user_uid")
    user_id = decoded.get("user_id")

    if not user_id:
        user_id = user_uid

    if not isinstance(user_uid, str) or not user_uid.strip():
        raise _unauthorized("Token missing user_uid")

    if (
            isinstance(user_id, bool)
            or not isinstance(user_id, (str, int))
            or not str(user_id).strip()
    ):
        raise _unauthorized("Token missing user_id")
    print(user_id, user_uid)
    return AuthenticatedUser(user_id=str(user_id), user_uid=user_uid)
