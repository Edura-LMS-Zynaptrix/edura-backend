from pydantic import BaseModel

class AdminBase(BaseModel):
    pass

class AdminResponse(AdminBase):
    id: int

    model_config = {
        "from_attributes": True
    }