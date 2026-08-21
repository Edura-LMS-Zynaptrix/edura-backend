import os
from typing import Callable, List

from fastapi import HTTPException, Request
import jwt
from jwt.exceptions import ExpiredSignatureError, PyJWTError

SECRET_KEY = os.getenv("SECRET_KEY", "edura_super_secret_jwt_key_2026_dev")
ALGORITHM = "HS256"
ASGARDEO_JWKS_URL = os.getenv("ASGARDEO_JWKS_URL", "")


def require_role(roles: List[str]) -> Callable:
    """
    Returns a FastAPI dependency that validates a Bearer JWT and enforces role membership.

    Primary usage — route protection via Depends():
        @router.delete("/x", dependencies=[Depends(require_role(["admin"]))])

    Injected payload usage:
        async def route(payload: dict = Depends(require_role(["student", "teacher", "admin"]))):
            user_id = int(payload["sub"])

    Standalone / internal call:
        payload = await require_role(["admin"])(request)

    Raises:
        HTTPException 401 MISSING_TOKEN   — no / malformed Authorization header
        HTTPException 401 TOKEN_EXPIRED   — JWT past its exp claim
        HTTPException 401 INVALID_TOKEN   — signature invalid or payload corrupt
        HTTPException 403 INSUFFICIENT_PERMISSIONS — role not in allowed list
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
            # Decode using HS256 key (or unverified/JWKS fallback if configured)
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
