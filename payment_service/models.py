"""
payment_service/models.py
SQLAlchemy 2.0 ORM models for the Payment Service.
Entities: Payment, PaymentReceipt, BankAccount

Matches ER diagram:
- payment entity: amount, reference_no, due_time, date_time
- PayHere automated + manual receipt upload (hybrid payment model)
- enroll_id linkage to enrollment_service.enrollments
"""

import enum

from database import Base  # shared Base from database.py
from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from shared.replicas import define_replica_course

# ---------------------------------------------------------------------------
# ENUMs  (AC2 from issue #06 — must be Python Enum, enforced at column level)
# ---------------------------------------------------------------------------


class PaymentMethod(str, enum.Enum):
    MANUAL = "MANUAL"  # Student uploads a bank slip / receipt
    PAYHERE = "PAYHERE"  # Automated PayHere gateway


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class ReceiptStatus(str, enum.Enum):
    PENDING = "PENDING"  # Awaiting admin review
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Payment(Base):
    """
    A payment record for a course purchase.
    Matches ER diagram: payment entity with amount, reference_no, due_time, date_time.
    enrollment_id links back to enrollment_service.enrollments (cross-service int ref).
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    enrollment_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    payment_method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, name="paymentmethod"), nullable=False
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="paymentstatus"),
        nullable=False,
        default=PaymentStatus.PENDING,
    )
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="LKR")
    reference_no: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    payhere_order_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    due_time: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_at: Mapped[DateTime | None] = mapped_column(
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
        # student_id/course_id already get an index from index=True above —
        # these duplicate Index() entries (same auto-generated name) made
        # create_all() fail with DuplicateTable on every single startup,
        # which aborted the whole call before payment_receipts (declared
        # after this table) ever got created.
        Index("ix_payments_status", "payment_status"),
    )


class PaymentReceipt(Base):
    """
    Manual payment receipt uploaded by the student (bank slip / cash receipt).
    Admin reviews and approves/rejects.
    Matches ER diagram: manual receipt flow mentioned in payment entity.
    """

    __tablename__ = "payment_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    receipt_url: Mapped[str] = mapped_column(String(512), nullable=False)
    receipt_public_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receipt_status: Mapped[ReceiptStatus] = mapped_column(
        Enum(ReceiptStatus, name="receiptstatus"),
        nullable=False,
        default=ReceiptStatus.PENDING,
    )
    reviewer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[DateTime | None] = mapped_column(
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

    # payment_id/student_id already get an index from index=True above —
    # no need for __table_args__ here (same duplicate-index bug as Payment).


class BankAccount(Base):
    """A platform bank account students deposit into before uploading a slip."""

    __tablename__ = "bank_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bank_name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_number: Mapped[str] = mapped_column(String(60), nullable=False)
    account_name: Mapped[str] = mapped_column(String(160), nullable=False)
    branch: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    order_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# What course_service announced about each course (price, status). Written only
# by the event consumer; a free-enrollment is checked against it.
ReplicaCourse = define_replica_course(Base)
