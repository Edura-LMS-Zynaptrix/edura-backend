from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, field_validator


class CourseCreate(BaseModel):
    title: str
    description: Optional[str] = None
    price: Decimal = Decimal("0.00")


class CourseUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    price: Optional[Decimal] = None
    status: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"DRAFT", "PUBLISHED", "ARCHIVED"}:
            raise ValueError("status must be DRAFT, PUBLISHED, or ARCHIVED")
        return v


class CourseResponse(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    price: Decimal
    teacher_id: int
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_course(cls, course) -> "CourseResponse":
        return cls(
            id=course.id,
            title=course.title,
            description=course.description,
            price=course.price,
            teacher_id=course.instructor_id,
            status=(
                course.status.value
                if hasattr(course.status, "value")
                else course.status
            ),
            created_at=course.created_at,
            updated_at=course.updated_at,
        )


class ModuleCreate(BaseModel):
    title: str
    order: int = 0


class ModuleResponse(BaseModel):
    id: int
    course_id: int
    title: str
    order: int
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_module(cls, module) -> "ModuleResponse":
        return cls(
            id=module.id,
            course_id=module.course_id,
            title=module.title,
            order=module.position,
            created_at=module.created_at,
        )


class LessonCreate(BaseModel):
    title: str
    youtube_video_id: str
    cloudinary_asset_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    order: int = 0


class LessonResponse(BaseModel):
    id: int
    module_id: int
    title: str
    youtube_video_id: Optional[str] = None
    cloudinary_asset_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    order: int
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_lesson(cls, lesson) -> "LessonResponse":
        return cls(
            id=lesson.id,
            module_id=lesson.module_id,
            title=lesson.title,
            youtube_video_id=lesson.youtube_video_id,
            cloudinary_asset_url=lesson.cloudinary_asset_url,
            duration_seconds=lesson.duration_seconds,
            order=lesson.position,
            created_at=lesson.created_at,
        )


class PaginatedCoursesResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[CourseResponse]
