from fastapi import APIRouter, Depends

from shared.auth import require_role

router = APIRouter()


@router.get("/course")
async def course_only(_: None = Depends(require_role(["course_manager"]))):
    return {"message": "Welcome Course Manager"}
