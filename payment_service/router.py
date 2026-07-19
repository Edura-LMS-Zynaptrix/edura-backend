from fastapi import APIRouter, Depends

from shared.auth import require_role

router = APIRouter()


@router.get("/payment")
async def payment_only(_: None = Depends(require_role(["payment_manager"]))):
    return {"message": "Welcome Payment Manager"}
