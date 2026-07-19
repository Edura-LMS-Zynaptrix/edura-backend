"""
admin_service/models.py
SQLAlchemy 2.0 ORM models for the Admin Service.
Entities: AuditLog

Matches ER diagram: admin entity (admin_id, password, email) — admin users are
stored in auth_service.users (role=admin). The admin_service itself owns only
the AuditLog table for tracking all administrative actions.
"""

import enum

from database import Base  # shared Base from database.py
from sqlalchemy import DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

# ---------------------------------------------------------------------------
# ENUMs
# ---------------------------------------------------------------------------


class AuditAction(str, enum.Enum):
    # User management
    user_created = "user_created"
    user_deactivated = "user_deactivated"
    user_role_changed = "user_role_changed"
    # Payment management
    receipt_approved = "receipt_approved"
    receipt_rejected = "receipt_rejected"
    # Course management
    course_published = "course_published"
    course_unpublished = "course_unpublished"
    course_deleted = "course_deleted"
    # Enrollment
    enrollment_revoked = "enrollment_revoked"
    # General
    settings_updated = "settings_updated"
    other = "other"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AuditLog(Base):
    """
    Immutable record of all administrative actions taken in the system.
    actor_id: the admin user who performed the action (ref auth_service.users).
    target_id: the entity affected (e.g. user_id, course_id, payment_id).
    target_type: the service/entity type ('user', 'course', 'payment', etc.).
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, name="auditaction"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
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
        Index("ix_audit_logs_actor_id", "actor_id"),
        Index("ix_audit_logs_target_type_id", "target_type", "target_id"),
    )
