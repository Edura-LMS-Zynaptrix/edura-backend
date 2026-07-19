from fastapi import APIRouter, Depends

from shared.auth import require_role

router = APIRouter()


@router.get("/notification")
async def notification_only(_: None = Depends(require_role(["notification_manager"]))):
    return {"message": "Welcome Notification Manager"}
