import os
from typing import Callable, List

import jwt
from fastapi import HTTPException, Request
from jwt.exceptions import ExpiredSignatureError, PyJWTError

SECRET_KEY = os.getenv("SECRET_KEY", "edura_super_secret_jwt_key_2026_dev")
ALGORITHM = "HS256"
ASGARDEO_JWKS_URL = os.getenv("ASGARDEO_JWKS_URL", "")


def require_role(roles: List[str]) -> Callable:
    """
    Returns a FastAPI dependency that validates a Bearer JWT and enforces role membership.
    """

    async def _dependency(request: Request) -> dict:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "MISSING_TOKEN",
                    "message": "Authorization header missing or malformed",
                },
            )

        token = auth_header[7:].strip()
        if not token:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "MISSING_TOKEN",
                    "message": "Authorization token is empty",
                },
            )

        try:
            payload = jwt.decode(
                token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_aud": False}
            )
        except ExpiredSignatureError:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "TOKEN_EXPIRED",
                    "message": "Access token has expired",
                },
            )
        except PyJWTError:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "INVALID_TOKEN",
                    "message": "Token could not be validated",
                },
            )

        role = str(payload.get("role", "")).lower()
        allowed_roles = [r.lower() for r in roles]

        if role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "INSUFFICIENT_PERMISSIONS",
                    "message": f"Role '{payload.get('role')}' is not permitted to access this resource",
                },
            )

        return payload

    return _dependency
