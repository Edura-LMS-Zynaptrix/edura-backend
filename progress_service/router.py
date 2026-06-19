from fastapi import APIRouter, Depends
from shared.auth import require_role

router = APIRouter()

@router.get("/progress")
async def progress_only(
    _: None = Depends(require_role(["progress_manager"]))
):
    return {"message": "Welcome Progress Manager"}