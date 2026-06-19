from pydantic import BaseModel

class NotificationBase(BaseModel):
    pass

class NotificationResponse(NotificationBase):
    id: int

    model_config = {
        "from_attributes": True
    }