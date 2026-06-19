from fastapi import APIRouter, Depends
from shared.auth import require_role

router = APIRouter()

@router.get("/user")
async def user_only(
    _: None = Depends(require_role(["user_manager"]))
):
    return {"message": "Welcome User Manager"}