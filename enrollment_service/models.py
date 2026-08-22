"""
enrollment_service/models.py
SQLAlchemy 2.0 ORM models for the Enrollment Service.
Entities: Enrollment
Matches ER diagram: enroll entity (enroll_id, student ↔ course relationship,
linked to payment via enroll_id).
"""

import enum

from database import Base  # shared Base from database.py
from sqlalchemy import DateTime, Enum, Index, Integer, UniqueConstraint
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
