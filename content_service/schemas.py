from pydantic import BaseModel

class ContentBase(BaseModel):
    pass

class ContentResponse(ContentBase):
    id: int

    model_config = {
        "from_attributes": True
    }