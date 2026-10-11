"""
course_service/models.py
SQLAlchemy 2.0 ORM models for the Course Service.
Entities: Course, Module, Lesson, Material, VideoCheckpoint, CheckpointQuestion
Matches ER diagram: course entity (desc_name, instructor/teacher relationship),
module structure, and lesson hierarchy.
"""

import enum

from database import Base  # shared Base from database.py
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
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
    # Nullable: existing courses created before this field was added stay
    # visible to every grade rather than being retroactively hidden.
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Only meaningful for grade 12/13 (A/L) courses — a course can belong to
    # more than one stream since some subjects (e.g. Physics, Chemistry) are
    # shared across streams. Stored as a plain JSON array of user_service
    # Stream ids (cross-service reference, no FK — streams live in a
    # different database). Empty/null = visible to every stream.
    stream_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[CourseStatus] = mapped_column(
        Enum(CourseStatus, name="coursestatusenum"),
        nullable=False,
        default=CourseStatus.DRAFT,
    )
    thumbnail_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    thumbnail_public_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    A lesson-group inside a course (shown to users as a "Lesson").
    Video courses hang videos (Lesson rows) and materials off it; exam
    courses hang assessments (assessment_service, by module_id) off it.
    """

    __tablename__ = "modules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_public_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    lesson_type: Mapped[LessonType] = mapped_column(
        Enum(LessonType, name="lessontype"), nullable=False, default=LessonType.video
    )
    video_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    youtube_video_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cloudinary_asset_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cloudinary_public_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
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


class Material(Base):
    """
    A downloadable resource (Cloudinary-hosted) inside a module, optionally
    pinned to one video (lesson_id). Several materials may share a video.
    """

    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False)
    module_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lesson_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    file_url: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)
    file_public_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_materials_module_id", "module_id"),
        Index("ix_materials_lesson_id", "lesson_id"),
    )


class VideoCheckpoint(Base):
    """A pause point inside a video (lesson) where the player asks practice questions."""

    __tablename__ = "video_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lesson_id: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_video_checkpoints_lesson_id", "lesson_id"),)


class CheckpointQuestion(Base):
    """An ungraded MCQ shown at a checkpoint; options a-b required, c-e optional."""

    __tablename__ = "checkpoint_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    checkpoint_id: Mapped[int] = mapped_column(Integer, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    option_a: Mapped[str] = mapped_column(String(500), nullable=False)
    option_b: Mapped[str] = mapped_column(String(500), nullable=False)
    option_c: Mapped[str | None] = mapped_column(String(500), nullable=True)
    option_d: Mapped[str | None] = mapped_column(String(500), nullable=True)
    option_e: Mapped[str | None] = mapped_column(String(500), nullable=True)
    correct_option: Mapped[str] = mapped_column(String(1), nullable=False)  # a-e
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_checkpoint_questions_checkpoint_id", "checkpoint_id"),)
