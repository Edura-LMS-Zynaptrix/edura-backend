from fastapi import APIRouter, Depends

from shared.auth import require_role

router = APIRouter()


@router.get("/content")
async def content_only(_: None = Depends(require_role(["content_manager"]))):
    return {"message": "Welcome Content Manager"}
