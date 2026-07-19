from fastapi import APIRouter, Depends

from shared.auth import require_role

router = APIRouter()


@router.get("/assessment")
async def assessment_only(_: None = Depends(require_role(["admin"]))):
    return {"message": "Welcome Admin"}
