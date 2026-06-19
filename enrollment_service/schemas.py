from pydantic import BaseModel

class EnrollmentBase(BaseModel):
    pass

class EnrollmentResponse(EnrollmentBase):
    id: int

    model_config = {
        "from_attributes": True
    }