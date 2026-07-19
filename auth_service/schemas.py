from pydantic import BaseModel


class UserBase(BaseModel):
    pass


class UserResponse(UserBase):
    id: int

    model_config = {"from_attributes": True}
