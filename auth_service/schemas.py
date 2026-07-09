from pydantic import BaseModel

class UserBase(BaseModel):
    pass

class UserResponse(UserBase):
    id: int

    model_config = {
        "from_attributes": True
    }

class AdminLogin(BaseModel):
    username: str
    password: str

class TeacherLogin(BaseModel):
    username: str
    password: str