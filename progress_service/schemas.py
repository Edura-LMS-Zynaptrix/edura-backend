from pydantic import BaseModel

class ProgressBase(BaseModel):
    pass

class ProgressResponse(ProgressBase):
    id: int

    model_config = {
        "from_attributes": True
    }