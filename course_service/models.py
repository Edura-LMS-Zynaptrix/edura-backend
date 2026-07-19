"""
course_service/models.py
SQLAlchemy 2.0 ORM models for the Course Service.
Entities: Course, Module, Lesson
Matches ER diagram: course entity (desc_name, instructor/teacher relationship),
module structure, and lesson hierarchy.
"""

import enum

from database import Base  # shared Base from database.py
from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

# ---------------------------------------------------------------------------
# ENUMs
# ---------------------------------------------------------------------------


class CourseType(str, enum.Enum):
    video = "video"
    exam = "exam"


class CourseStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class LessonType(str, enum.Enum):
    video = "video"
    document = "document"
    quiz = "quiz"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Course(Base):
    """
    Represents a course created by a teacher.
    Matches ER diagram: course entity with desc_name, linked to teacher via instructor_id.
    """

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instructor_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    course_type: Mapped[CourseType] = mapped_column(
        Enum(CourseType, name="coursetype"), nullable=False, default=CourseType.video
    )
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0.00)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[CourseStatus] = mapped_column(
        Enum(CourseStatus, name="coursestatusenum"),
        nullable=False,
        default=CourseStatus.DRAFT,
    )
    thumbnail_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (Index("ix_courses_instructor_id", "instructor_id"),)


class Module(Base):
    """
    A named section/chapter within a course.
    Ordered by position for display.
    """

    __tablename__ = "modules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (Index("ix_modules_course_id", "course_id"),)


class Lesson(Base):
    """
    An individual lesson within a module.
    Matches ER diagram: lesson/video content within a course.
    duration_seconds: length of the video/content in seconds.
    """

    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    module_id: Mapped[int] = mapped_column(Integer, nullable=False)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    lesson_type: Mapped[LessonType] = mapped_column(
        Enum(LessonType, name="lessontype"), nullable=False, default=LessonType.video
    )
    video_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    youtube_video_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cloudinary_asset_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_free_preview: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_lessons_module_id", "module_id"),
        Index("ix_lessons_course_id", "course_id"),
    )
