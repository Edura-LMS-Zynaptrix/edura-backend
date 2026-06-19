from pydantic import BaseModel

class AnalyticsBase(BaseModel):
    pass

class AnalyticsResponse(AnalyticsBase):
    id: int

    model_config = {
        "from_attributes": True
    }