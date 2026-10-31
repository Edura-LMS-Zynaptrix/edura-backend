"""Payment events: other services act on these, so they carry everything needed."""

from datetime import datetime, timezone

from shared import events as ev
from shared.messaging import publish


def publish_payment_success(
    student_id: int,
    course_id: int,
    order_id: str,
    amount: float,
    payment_id: int | None = None,
    paid_at: datetime | None = None,
):
    """A payment went through (PayHere, an approved bank slip, or a free course)."""
    publish(
        ev.PAYMENT_SUCCESS,
        {
            "student_id": student_id,
            "course_id": course_id,
            "order_id": order_id,
            "amount": amount,
            "payment_id": payment_id,
            "completed_at": (paid_at or datetime.now(timezone.utc)).isoformat(),
        },
    )


def publish_receipt_uploaded(receipt_id: int, payment_id: int, student_id: int):
    publish(
        ev.PAYMENT_RECEIPT_UPLOADED,
        {"receipt_id": receipt_id, "payment_id": payment_id, "student_id": student_id},
    )


def publish_receipt_reviewed(
    receipt_id: int, student_id: int, status: str, reviewed_at: datetime | None
):
    publish(
        ev.PAYMENT_RECEIPT_REVIEWED,
        {
            "receipt_id": receipt_id,
            "student_id": student_id,
            "status": status,
            "reviewed_at": reviewed_at.isoformat() if reviewed_at else None,
        },
    )
