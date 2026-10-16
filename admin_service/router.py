import os
from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, Depends

from shared.auth import require_role

router = APIRouter()

ASSESSMENT_SERVICE_URL = os.getenv(
    "ASSESSMENT_SERVICE_URL", "http://assessment_service:8007"
).rstrip("/")


@router.get("/admin")
async def admin_only(_: None = Depends(require_role(["admin"]))):
    return {"message": "Welcome Admin"}


@router.get("/violations")
@router.get("/api/admin/violations")
async def list_violations(_: None = Depends(require_role(["admin"]))):
    """
    Returns violation logs by requesting assessment_service.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ASSESSMENT_SERVICE_URL}/violations")
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return []
