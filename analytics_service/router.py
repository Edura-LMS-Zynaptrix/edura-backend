from fastapi import APIRouter, Depends

from shared.auth import require_role

router = APIRouter()


@router.get("/analytics")
async def analytics_only(_: None = Depends(require_role(["admin"]))):
    return {"message": "Welcome Admin"}
