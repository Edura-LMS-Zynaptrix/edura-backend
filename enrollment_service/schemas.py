from datetime import datetime

from models import EnrollmentStatus
from pydantic import BaseModel, Field


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
    # Set only for a single-lesson grant; None means the whole course.
    module_id: int | None = None
    is_manual: bool = False

    @classmethod
    def from_orm_model(cls, enrollment):
        return cls(
            enrollment_id=enrollment.id,
            student_id=enrollment.student_id,
            course_id=enrollment.course_id,
            status=enrollment.status,
            enrolled_at=enrollment.enrolled_at,
            expires_at=enrollment.expires_at,
            is_manual=bool(enrollment.is_manual),
        )

    @classmethod
    def from_lesson_access(cls, access):
        return cls(
            enrollment_id=access.id,
            student_id=access.student_id,
            course_id=access.course_id,
            status=EnrollmentStatus.ACTIVE,
            enrolled_at=access.created_at,
            module_id=access.module_id,
            is_manual=True,
        )

    model_config = {"from_attributes": True, "populate_by_name": True}


class AccessGrantRequest(BaseModel):
    course_id: int
    module_id: int | None = None  # None = the whole course
    user_ids: list[int] = Field(..., min_length=1, max_length=200)


class AccessStudent(BaseModel):
    user_id: str
    full_name: str
    grade: int | None = None
    district: str | None = None
    school: str | None = None


class AccessEnrolledStudent(BaseModel):
    user_id: str
    full_name: str
    enrolled_at: datetime
    is_manual: bool


class ClassPackageCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    description: str | None = None
    grade: int = Field(..., ge=5, le=13)
    stream_id: int | None = None
    price: float = Field(0, ge=0)
    is_active: bool = True


class ClassPackageUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = None
    grade: int | None = Field(default=None, ge=5, le=13)
    stream_id: int | None = None
    price: float | None = Field(default=None, ge=0)
    is_active: bool | None = None


class ClassEnrollmentGrant(BaseModel):
    user_id: int
    class_package_id: int
    expires_at: datetime | None = None
