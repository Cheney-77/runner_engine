import jwt
import secrets

from fastapi import HTTPException


class Passport:
    sk = None

    @classmethod
    def init(cls, secret: str):
        cls.sk = secret

    @classmethod
    def issue(cls, payload):
        return jwt.encode(
            payload,
            cls.sk,
            algorithm="HS256"
        )

    @classmethod
    def verify(cls, token: str, *, secret: str) -> dict:

        try:
            return jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                options={"require": ["exp"]},
            )

        except jwt.exceptions.InvalidSignatureError:
            raise HTTPException(
                status_code=401,
                detail="Invalid token signature"
            )

        except jwt.exceptions.DecodeError:
            raise HTTPException(
                status_code=401,
                detail="Invalid token"
            )

        except jwt.exceptions.ExpiredSignatureError:
            raise HTTPException(
                status_code=401,
                detail="Token expired"
            )

    @staticmethod
    def generate_refresh_token(
            length: int = 64
    ):
        return secrets.token_hex(length)
