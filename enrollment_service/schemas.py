from datetime import datetime

from models import EnrollmentStatus
from pydantic import BaseModel


class EnrollmentBase(BaseModel):
    student_id: int
    course_id: int


class EnrollmentResponse(BaseModel):
    enrollment_id: int
    student_id: int
    course_id: int
    status: EnrollmentStatus
    enrolled_at: datetime
    expires_at: datetime | None = None

    @classmethod
    def from_orm_model(cls, enrollment):
        return cls(
            enrollment_id=enrollment.id,
            student_id=enrollment.student_id,
            course_id=enrollment.course_id,
            status=enrollment.status,
            enrolled_at=enrollment.enrolled_at,
            expires_at=enrollment.expires_at,
        )

    model_config = {"from_attributes": True, "populate_by_name": True}
