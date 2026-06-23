"""
progress_service/models.py
SQLAlchemy 2.0 ORM models for the Progress Service.
Entities: LessonProgress, CourseProgress, Certificate, LeaderboardEntry

Matches ER diagram:
- Progress tracking referenced in student dashboard features (lesson resume, completion).
- Leaderboard (gamification) referenced in the Logged-in User Leader Board figure.
- Certificate generated on course completion.
"""
import enum

from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database import Base  # shared Base from database.py


# ---------------------------------------------------------------------------
# ENUMs
# ---------------------------------------------------------------------------

class CourseProgressStatus(str, enum.Enum):
    in_progress = "in_progress"
    completed = "completed"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class LessonProgress(Base):
    """
    Tracks whether a student has completed an individual lesson.
    Supports the 'resume lesson' feature confirmed in client feedback.
    """
    __tablename__ = "lesson_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    lesson_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    is_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    watch_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_position_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("student_id", "lesson_id", name="uq_lesson_progress_student_lesson"),
        Index("ix_lesson_progress_student_id", "student_id"),
        Index("ix_lesson_progress_lesson_id", "lesson_id"),
        Index("ix_lesson_progress_course_id", "course_id"),
    )


class CourseProgress(Base):
    """
    Aggregate progress of a student through an entire course.
    completed_lessons_count updated by events from lesson_progress.
    """
    __tablename__ = "course_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[CourseProgressStatus] = mapped_column(
        Enum(CourseProgressStatus, name="courseprogressstatus"),
        nullable=False,
        default=CourseProgressStatus.in_progress,
    )
    completed_lessons_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_lessons_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_percentage: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0.00)
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("student_id", "course_id", name="uq_course_progress_student_course"),
        Index("ix_course_progress_student_id", "student_id"),
        Index("ix_course_progress_course_id", "course_id"),
    )


class Certificate(Base):
    """
    A certificate of completion issued when a student finishes a course.
    certificate_url points to the generated PDF stored in cloud storage.
    """
    __tablename__ = "certificates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    certificate_url: Mapped[str] = mapped_column(String(512), nullable=False)
    issued_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("student_id", "course_id", name="uq_certificate_student_course"),
        Index("ix_certificates_student_id", "student_id"),
        Index("ix_certificates_course_id", "course_id"),
    )


class LeaderboardEntry(Base):
    """
    Gamification leaderboard entry per course.
    Matches ER diagram: leaderboard figures (Logged-in User Leader Board).
    points accumulated from completed lessons, assessments, and streaks.
    """
    __tablename__ = "leaderboard_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    streak_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_activity_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("student_id", "course_id", name="uq_leaderboard_student_course"),
        Index("ix_leaderboard_entries_student_id", "student_id"),
        Index("ix_leaderboard_entries_course_id", "course_id"),
        Index("ix_leaderboard_entries_points", "points"),
    )