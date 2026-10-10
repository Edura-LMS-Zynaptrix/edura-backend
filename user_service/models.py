"""
user_service/models.py
SQLAlchemy 2.0 ORM models for the User Service.
Entities: UserProfile
The user_service stores extended profile data beyond auth credentials.
Matches ER diagram: student (first_name, last_name, DOB, mobile_no) and
teacher (first_name, last_name, mobile_no) profile attributes.
"""

import enum
from datetime import date

from database import Base  # shared Base from database.py
from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

# ---------------------------------------------------------------------------
# ENUMs
# ---------------------------------------------------------------------------


class ProfileRole(str, enum.Enum):
    student = "student"
    teacher = "teacher"
    admin = "admin"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class UserProfile(Base):
    """
    Extended profile data for all user roles.
    user_id is a plain int (FK to auth_service.users.id — cross-service, no SQLAlchemy FK).
    Matches ER diagram: first_name, last_name, mobile_no, DOB (student), email stored in auth.
    """

    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    role: Mapped[ProfileRole] = mapped_column(
        Enum(ProfileRole, name="profilerole"),
        nullable=False,
        default=ProfileRole.student,
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    mobile_no: Mapped[str | None] = mapped_column(String(20), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    avatar_public_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── Student academic profile (mirrors the reference exam-prep platform's
    # registration fields; only meaningful for role=student) ────────────────
    school: Mapped[str | None] = mapped_column(String(200), nullable=True)
    district: Mapped[str | None] = mapped_column(String(100), nullable=True)
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stream_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nic_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    selected_subjects: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # ── Referrals: each profile gets its own shareable code; referred_by_user_id
    # is set once, at profile creation, from the code the new user entered ────
    referral_code: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    referred_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Earned when someone you referred first enrolls (display-only, as in the reference platform).
    wallet_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_credits_earned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_successful_referrals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (Index("ix_user_profiles_user_id", "user_id"),)


class Stream(Base):
    """Educational stream (e.g. Biology, Maths, Commerce) for grade 12/13
    students — groups the subjects available to pick from at that level."""

    __tablename__ = "streams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    subjects: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class GradeSubject(Base):
    """Subject catalogue for grades 5-11 (below stream level)."""

    __tablename__ = "grade_subjects"
    __table_args__ = (
        UniqueConstraint("name", "grade", name="uq_grade_subjects_name_grade"),
        Index("ix_grade_subjects_grade", "grade"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    grade: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReferralEvent(Base):
    """
    One referral: created "pending" when a code is applied, flipped to
    "credited" when the referred student first enrolls in a course.
    A user can only ever be referred once (referred_user_id is unique).
    """

    __tablename__ = "referral_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    referred_user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    credit_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    qualified_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    credited_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WalletTransaction(Base):
    """Ledger of credit changes, so a balance can always be explained."""

    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    transaction_type: Mapped[str] = mapped_column(String(30), nullable=False)  # referral_reward | manual_adjustment | spend
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

