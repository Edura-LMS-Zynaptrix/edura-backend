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

from sqlalchemy import Date, DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database import Base  # shared Base from database.py


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
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    role: Mapped[ProfileRole] = mapped_column(
        Enum(ProfileRole, name="profilerole"), nullable=False, default=ProfileRole.student
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    mobile_no: Mapped[str | None] = mapped_column(String(20), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_user_profiles_user_id", "user_id"),
    )