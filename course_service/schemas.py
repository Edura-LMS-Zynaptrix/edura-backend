from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, field_validator


class CourseCreate(BaseModel):
    title: str
    description: Optional[str] = None
    price: Decimal = Decimal("0.00")
    course_type: str = "video"
    grade: Optional[int] = None
    stream_ids: Optional[list[int]] = None
    thumbnail_url: Optional[str] = None
    thumbnail_public_id: Optional[str] = None

    @field_validator("course_type")
    @classmethod
    def validate_course_type(cls, v: str) -> str:
        if v not in {"video", "exam"}:
            raise ValueError("course_type must be 'video' or 'exam'")
        return v

    @field_validator("grade")
    @classmethod
    def validate_grade(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (5 <= v <= 13):
            raise ValueError("grade must be between 5 and 13")
        return v


class CourseUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    price: Optional[Decimal] = None
    course_type: Optional[str] = None
    grade: Optional[int] = None
    stream_ids: Optional[list[int]] = None
    status: Optional[str] = None
    thumbnail_url: Optional[str] = None
    thumbnail_public_id: Optional[str] = None

    @field_validator("course_type")
    @classmethod
    def validate_course_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"video", "exam"}:
            raise ValueError("course_type must be 'video' or 'exam'")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"DRAFT", "PUBLISHED", "ARCHIVED"}:
            raise ValueError("status must be DRAFT, PUBLISHED, or ARCHIVED")
        return v

    @field_validator("grade")
    @classmethod
    def validate_grade(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (5 <= v <= 13):
            raise ValueError("grade must be between 5 and 13")
        return v


class CourseResponse(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    price: Decimal
    course_type: str
    grade: Optional[int] = None
    stream_ids: Optional[list[int]] = None
    teacher_id: int
    status: str
    thumbnail_url: Optional[str] = None
    thumbnail_public_id: Optional[str] = None
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
            course_type=(
                course.course_type.value
                if hasattr(course.course_type, "value")
                else course.course_type
            ),
            grade=course.grade,
            stream_ids=course.stream_ids,
            teacher_id=course.instructor_id,
            status=(
                course.status.value
                if hasattr(course.status, "value")
                else course.status
            ),
            thumbnail_url=course.thumbnail_url,
            thumbnail_public_id=course.thumbnail_public_id,
            created_at=course.created_at,
            updated_at=course.updated_at,
        )


class ModuleCreate(BaseModel):
    title: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    image_public_id: Optional[str] = None
    is_active: bool = True
    order: int = 0


class ModuleUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    image_public_id: Optional[str] = None
    is_active: Optional[bool] = None
    order: Optional[int] = None


class ModuleResponse(BaseModel):
    id: int
    course_id: int
    title: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    image_public_id: Optional[str] = None
    is_active: bool = True
    order: int
    lesson_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_module(cls, module, lesson_count: int = 0) -> "ModuleResponse":
        return cls(
            id=module.id,
            course_id=module.course_id,
            title=module.title,
            description=module.description,
            image_url=module.image_url,
            image_public_id=module.image_public_id,
            is_active=module.is_active if module.is_active is not None else True,
            order=module.position,
            lesson_count=lesson_count,
            created_at=module.created_at,
        )


class LessonCreate(BaseModel):
    title: str
    description: Optional[str] = None
    youtube_video_id: str
    cloudinary_asset_url: Optional[str] = None
    cloudinary_public_id: Optional[str] = None
    duration_seconds: Optional[int] = None
    order: int = 0


class LessonUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    youtube_video_id: Optional[str] = None
    cloudinary_asset_url: Optional[str] = None
    cloudinary_public_id: Optional[str] = None
    duration_seconds: Optional[int] = None
    order: Optional[int] = None


class CheckpointQuestionResponse(BaseModel):
    id: int
    checkpoint_id: int
    question_text: str
    option_a: str
    option_b: str
    option_c: Optional[str] = None
    option_d: Optional[str] = None
    option_e: Optional[str] = None
    correct_option: str
    explanation: Optional[str] = None
    order_number: int = 0

    model_config = {"from_attributes": True}


class CheckpointResponse(BaseModel):
    id: int
    video_id: int
    timestamp_seconds: int
    questions: list[CheckpointQuestionResponse] = []


class CheckpointUpdate(BaseModel):
    timestamp_seconds: int


class CheckpointQuestionUpdate(BaseModel):
    question_text: Optional[str] = None
    option_a: Optional[str] = None
    option_b: Optional[str] = None
    option_c: Optional[str] = None
    option_d: Optional[str] = None
    option_e: Optional[str] = None
    correct_option: Optional[str] = None
    explanation: Optional[str] = None
    order_number: Optional[int] = None

    @field_validator("correct_option")
    @classmethod
    def validate_correct_option(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.lower() not in {"a", "b", "c", "d", "e"}:
            raise ValueError("correct_option must be a, b, c, d or e")
        return v.lower() if v else v


class LessonResponse(BaseModel):
    id: int
    module_id: int
    title: str
    description: Optional[str] = None
    youtube_video_id: Optional[str] = None
    cloudinary_asset_url: Optional[str] = None
    cloudinary_public_id: Optional[str] = None
    duration_seconds: Optional[int] = None
    order: int
    created_at: datetime
    checkpoints: list[CheckpointResponse] = []

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_lesson(cls, lesson) -> "LessonResponse":
        return cls(
            id=lesson.id,
            module_id=lesson.module_id,
            title=lesson.title,
            description=lesson.description,
            youtube_video_id=lesson.youtube_video_id,
            cloudinary_asset_url=lesson.cloudinary_asset_url,
            cloudinary_public_id=lesson.cloudinary_public_id,
            duration_seconds=lesson.duration_seconds,
            order=lesson.position,
            created_at=lesson.created_at,
        )


class MaterialCreate(BaseModel):
    title: str
    file_url: str
    file_type: str
    file_public_id: Optional[str] = None
    lesson_id: Optional[int] = None
    order: int = 0


class MaterialUpdate(BaseModel):
    title: Optional[str] = None
    file_url: Optional[str] = None
    file_type: Optional[str] = None
    file_public_id: Optional[str] = None
    lesson_id: Optional[int] = None
    order: Optional[int] = None


class MaterialResponse(BaseModel):
    id: int
    course_id: int
    module_id: int
    lesson_id: Optional[int] = None
    title: str
    file_url: str
    file_type: str
    file_public_id: Optional[str] = None
    order: int
    created_at: datetime

    @classmethod
    def from_orm_material(cls, material) -> "MaterialResponse":
        return cls(
            id=material.id,
            course_id=material.course_id,
            module_id=material.module_id,
            lesson_id=material.lesson_id,
            title=material.title,
            file_url=material.file_url,
            file_type=material.file_type,
            file_public_id=material.file_public_id,
            order=material.position,
            created_at=material.created_at,
        )


class CourseWithModulesResponse(CourseResponse):
    # None when the caller didn't ask for include_modules (vs [] = no modules).
    modules: Optional[list[ModuleResponse]] = None


class PaginatedCoursesResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[CourseWithModulesResponse]
