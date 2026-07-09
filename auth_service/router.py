from fastapi import APIRouter, Depends
from shared.auth import require_role

router = APIRouter()

@router.get("/admin")
async def admin_only(
    _: None = Depends(require_role(["admin"]))
):
    return {"message": "Welcome Admin"}

@router.post("/admin")
async def admin_login():
    return {
        "login success"
    }