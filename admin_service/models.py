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
from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from shared.replicas import define_replica_user

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
        # actor_id already gets an index from index=True above — this
        # duplicate Index() (same auto-generated name) made create_all()
        # fail with DuplicateTable on every startup.
        Index("ix_audit_logs_target_type_id", "target_type", "target_id"),
    )


class Feedback(Base):
    """
    A suggestion / bug report / other note sent from the site footer form.
    Guests may submit too, so user_id (auth_service user) is optional and the
    contact details are stored as given.
    """

    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ChatConversation(Base):
    """
    One support thread. A guest thread is keyed by the browser's random
    session_id; once that guest signs in, sync attaches it to their user_id.
    """

    __tablename__ = "chat_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_message_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_from_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sender_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# What auth_service and user_service announced about each person (name, email).
# Written only by the event consumer.
ReplicaUser = define_replica_user(Base)
