"""
notification_service/models.py
SQLAlchemy 2.0 ORM models for the Notification Service.
Entities: NotificationLog

Stores a log of all notifications sent to users (email, in-app, push).
Matches ER diagram: notification flows triggered by payment, enrollment, assessment events.
"""

import enum

from database import Base  # shared Base from database.py
from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

# ---------------------------------------------------------------------------
# ENUMs
# ---------------------------------------------------------------------------


class NotificationChannel(str, enum.Enum):
    in_app = "in_app"
    email = "email"
    push = "push"


class NotificationStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class NotificationEvent(str, enum.Enum):
    enrollment_confirmed = "enrollment_confirmed"
    payment_received = "payment_received"
    payment_rejected = "payment_rejected"
    assessment_graded = "assessment_graded"
    certificate_issued = "certificate_issued"
    course_published = "course_published"
    otp_requested = "otp_requested"
    proctoring_violation = "proctoring_violation"
    general = "general"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class NotificationLog(Base):
    """
    Immutable log of every notification dispatched.
    Supports in-app read/unread state via is_read.
    recipient_id references auth_service.users.id (cross-service int ref).
    """

    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipient_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, index=True
    )
    recipient_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_type: Mapped[NotificationEvent] = mapped_column(
        Enum(NotificationEvent, name="notificationevent"), nullable=False
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel, name="notificationchannel"),
        nullable=False,
        default=NotificationChannel.in_app,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name="notificationstatus"),
        nullable=False,
        default=NotificationStatus.pending,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reference_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # e.g. payment_id / enrollment_id
    sent_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    read_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        Index("ix_notification_logs_status", "status"),
        Index("ix_notification_logs_is_read", "is_read"),
    )
