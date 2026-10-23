"""
analytics_service/models.py

Analytics owns no business data. Everything here is a read model: a copy of what
other services announced as events, stored so reports can be built without
asking anyone. Only ``event_consumer`` writes these tables.

  replica_users / _courses / _modules / _access   -- people, catalogue, who can open what
  replica_enrollments                              -- active course enrollments (for counts)
  replica_exams / replica_attempts / replica_badges -- exams, graded attempts, badges earned
  replica_payments / replica_receipts              -- completed payments, bank-slip reviews
"""

from datetime import datetime

from database import Base
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.replicas import (
    define_replica_access,
    define_replica_course,
    define_replica_module,
    define_replica_user,
)

ReplicaUser = define_replica_user(Base)
ReplicaCourse = define_replica_course(Base)
ReplicaModule = define_replica_module(Base)
ReplicaAccess = define_replica_access(Base)


class ReplicaEnrollment(Base):
    __tablename__ = "replica_enrollments"

    student_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False
    )
    course_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")


class ReplicaExam(Base):
    __tablename__ = "replica_exams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    module_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ReplicaAttempt(Base):
    __tablename__ = "replica_attempts"

    submission_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False
    )
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    exam_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    course_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    module_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    marks_obtained: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_questions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    time_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_replica_attempts_student_exam", "student_id", "exam_id"),
    )


class ReplicaBadge(Base):
    __tablename__ = "replica_badges"

    student_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False
    )
    badge_key: Mapped[str] = mapped_column(String(60), primary_key=True)


class ReplicaPayment(Base):
    __tablename__ = "replica_payments"

    order_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False)
    course_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ReplicaReceipt(Base):
    __tablename__ = "replica_receipts"

    receipt_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False
    )
    student_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
