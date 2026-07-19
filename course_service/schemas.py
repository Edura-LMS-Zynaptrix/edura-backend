from pydantic import BaseModel


class CourseBase(BaseModel):
    pass


class CourseResponse(CourseBase):
    id: int

    model_config = {"from_attributes": True}
