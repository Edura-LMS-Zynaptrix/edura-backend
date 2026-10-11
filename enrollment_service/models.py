"""
enrollment_service/models.py
SQLAlchemy 2.0 ORM models for the Enrollment Service.
Entities: Enrollment, LessonAccess, ClassPackage, ClassEnrollment
Matches ER diagram: enroll entity (enroll_id, student ↔ course relationship,
linked to payment via enroll_id).
"""

import enum

from database import Base  # shared Base from database.py
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

# ---------------------------------------------------------------------------
# ENUMs
# ---------------------------------------------------------------------------


class EnrollmentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Enrollment(Base):
    """
    Records a student's enrollment in a course.
    Matches ER diagram: enroll entity with enroll_id; student ↔ course many-to-many.
    Cross-service references (student_id → auth_service.users, course_id → course_service.courses)
    are stored as plain Integer columns — no SQLAlchemy ForeignKey across services.
    """

    __tablename__ = "enrollments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[EnrollmentStatus] = mapped_column(
        Enum(EnrollmentStatus, name="enrollmentstatus"),
        nullable=False,
        default=EnrollmentStatus.ACTIVE,
    )
    enrolled_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Granted by an admin rather than bought; such enrollments never expire.
    is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
        # A student can only enroll once per course (active or cancelled)
        UniqueConstraint(
            "student_id", "course_id", name="uq_enrollment_student_course"
        ),
        Index("ix_enrollments_status", "status"),
    )


class LessonAccess(Base):
    """
    Admin-granted access to a single lesson (course_service module) without
    enrolling in the whole course. A course enrollment always covers every lesson.
    """

    __tablename__ = "lesson_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    module_id: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "student_id", "module_id", name="uq_lesson_access_student_module"
        ),
    )


class ClassPackage(Base):
    """
    A grade-level bundle: enrolling in it unlocks every course it covers
    (see package_access.package_covers). stream_id is a user_service stream.
    """

    __tablename__ = "class_packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    grade: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    stream_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )


class ClassEnrollment(Base):
    """A student holding a class package (one row per student and package)."""

    __tablename__ = "class_enrollments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    class_package_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    payment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    enrolled_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "class_package_id", name="uq_class_enrollment_user_package"
        ),
    )
