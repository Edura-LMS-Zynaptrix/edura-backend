from pydantic import BaseModel

class AssessmentBase(BaseModel):
    pass

class AssessmentResponse(AssessmentBase):
    id: int

    model_config = {
        "from_attributes": True
    }