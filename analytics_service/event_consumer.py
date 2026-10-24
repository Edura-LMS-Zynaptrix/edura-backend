"""analytics_service's queue: every event it builds reports from.

Each handler sets a row to the state the event announces, so a message that
arrives twice does no harm.
"""

import logging
from datetime import datetime, timezone

from database import SessionLocal
from models import (
    ReplicaAccess,
    ReplicaAttempt,
    ReplicaBadge,
    ReplicaCourse,
    ReplicaEnrollment,
    ReplicaExam,
    ReplicaModule,
    ReplicaPayment,
    ReplicaReceipt,
    ReplicaUser,
)

from shared import events as ev
from shared import messaging, replicas

logger = logging.getLogger(__name__)

QUEUE = "analytics.queue"


def _time(value) -> datetime | None:
    return replicas.parse_time(value)


def _enrollment_changed(db, data):
    row = db.get(ReplicaEnrollment, (int(data["student_id"]), int(data["course_id"])))
    if row is None:
        row = ReplicaEnrollment(
            student_id=int(data["student_id"]), course_id=int(data["course_id"])
        )
        db.add(row)
    row.status = str(data.get("status") or "ACTIVE").upper()


def _assessment_upserted(db, data):
    row = db.get(ReplicaExam, int(data["id"]))
    if row is None:
        row = ReplicaExam(id=int(data["id"]))
        db.add(row)
    row.course_id = int(data["course_id"])
    row.module_id = data.get("module_id")
    row.title = data.get("title") or ""
    row.position = data.get("position") or 0
    row.is_published = bool(data.get("is_published"))
    row.created_at = _time(data.get("created_at"))


def _assessment_deleted(db, data):
    exam_id = int(data["id"])
    db.query(ReplicaAttempt).filter(ReplicaAttempt.exam_id == exam_id).delete()
    row = db.get(ReplicaExam, exam_id)
    if row is not None:
        db.delete(row)


def _assessment_graded(db, data):
    if data.get("submission_id") is None:
        return  # an older event without the full result; the resync sends it again
    row = db.get(ReplicaAttempt, int(data["submission_id"]))
    if row is None:
        row = ReplicaAttempt(submission_id=int(data["submission_id"]))
        db.add(row)
    row.student_id = int(data["student_id"])
    row.exam_id = int(data["assessment_id"])
    row.course_id = data.get("course_id")
    row.module_id = data.get("module_id")
    row.score = float(data.get("score") or 0)
    row.passed = bool(data.get("passed"))
    row.marks_obtained = int(data.get("marks_obtained") or 0)
    row.total_questions = int(data.get("total_questions") or 0)
    row.time_seconds = int(data.get("time_seconds") or 0)
    row.submitted_at = _time(data.get("submitted_at"))


def _badge_awarded(db, data):
    key = (int(data["student_id"]), str(data["badge_key"]))
    if data.get("badge_key") and db.get(ReplicaBadge, key) is None:
        db.add(ReplicaBadge(student_id=key[0], badge_key=key[1]))


def _payment_success(db, data):
    order_id = str(data.get("order_id") or "")
    if not order_id:
        return
    row = db.get(ReplicaPayment, order_id)
    if row is None:
        row = ReplicaPayment(order_id=order_id)
        db.add(row)
    row.student_id = int(data["student_id"])
    row.course_id = data.get("course_id")
    row.amount = float(data.get("amount") or 0)
    row.completed_at = _time(data.get("completed_at")) or datetime.now(timezone.utc)


def _receipt_uploaded(db, data):
    receipt_id = int(data["receipt_id"])
    if db.get(ReplicaReceipt, receipt_id) is None:
        db.add(
            ReplicaReceipt(
                receipt_id=receipt_id,
                student_id=int(data["student_id"]),
                status="PENDING",
            )
        )


def _receipt_reviewed(db, data):
    receipt_id = int(data["receipt_id"])
    row = db.get(ReplicaReceipt, receipt_id)
    if row is None:
        row = ReplicaReceipt(receipt_id=receipt_id, student_id=int(data["student_id"]))
        db.add(row)
    row.status = str(data.get("status") or "").upper()
    row.reviewed_at = _time(data.get("reviewed_at"))


_OWN = {
    ev.ENROLLMENT_CHANGED: _enrollment_changed,
    ev.ASSESSMENT_UPSERTED: _assessment_upserted,
    ev.ASSESSMENT_DELETED: _assessment_deleted,
    ev.ASSESSMENT_GRADED: _assessment_graded,
    ev.BADGE_AWARDED: _badge_awarded,
    ev.PAYMENT_SUCCESS: _payment_success,
    ev.PAYMENT_RECEIPT_UPLOADED: _receipt_uploaded,
    ev.PAYMENT_RECEIPT_REVIEWED: _receipt_reviewed,
}


def handle(key: str, data: dict) -> None:
    db = SessionLocal()
    try:
        handler = _OWN.get(key)
        if handler is not None:
            handler(db, data)
            db.commit()
        else:
            replicas.apply(
                db,
                key,
                data,
                user=ReplicaUser,
                course=ReplicaCourse,
                module=ReplicaModule,
                access=ReplicaAccess,
            )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def start() -> None:
    messaging.start_consumer("analytics", QUEUE, ev.QUEUES[QUEUE], handle)
