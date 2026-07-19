from fastapi import APIRouter, Depends

from shared.auth import require_role

router = APIRouter()


@router.get("/enrollment")
async def enrollment_only(_: None = Depends(require_role(["enrollment_manager"]))):
    return {"message": "Welcome Enrollment Manager"}
