"""enrollment_service's queue: payments (to enroll), catalogue and people (the copies it keeps)."""

import logging

from access_events import (
    announce_access,
    announce_access_many,
    announce_enrollment,
    students_with_packages,
)
from database import SessionLocal
from events import publish_enrollment_activated
from models import ReplicaCourse, ReplicaStream, ReplicaUser
from package_access import get_course
from services import activate_or_extend_enrollment, suspend_enrollment

from shared import events as ev
from shared import messaging, replicas

logger = logging.getLogger(__name__)

QUEUE = "enrollment.queue"


def _payment_success(db, data: dict) -> None:
    student_id, course_id = data.get("student_id"), data.get("course_id")
    if not (student_id and course_id):
        return
    enrollment = activate_or_extend_enrollment(db, student_id, course_id)
    logger.info(
        "Activated/extended enrollment: student %s, course %s", student_id, course_id
    )
    publish_enrollment_activated(
        enrollment_id=enrollment.id,
        student_id=enrollment.student_id,
        course_id=enrollment.course_id,
        expires_at=enrollment.expires_at,
    )
    announce_enrollment(enrollment, "payment")
    announce_access(db, student_id)


def _subscription_expired(db, data: dict) -> None:
    student_id, course_id = data.get("student_id"), data.get("course_id")
    if not (student_id and course_id):
        return
    enrollment = suspend_enrollment(db, student_id, course_id)
    logger.info("Suspended enrollment: student %s, course %s", student_id, course_id)
    if enrollment is not None:
        announce_enrollment(enrollment, "expiry")
    announce_access(db, student_id)


def _course_changed(db, key: str, data: dict) -> None:
    """Update the catalogue copy; if what a class package looks at changed, re-announce access."""
    course_id = int(data["id"])
    before = get_course(db, course_id)
    replicas.apply(db, key, data, course=ReplicaCourse)
    after = get_course(db, course_id)

    def coverage(course):
        return (
            None
            if course is None
            else (course["status"], course["grade"], course["stream_ids"])
        )

    if coverage(before) != coverage(after):
        announce_access_many(db, students_with_packages(db))


def handle(key: str, data: dict) -> None:
    db = SessionLocal()
    try:
        if key == ev.PAYMENT_SUCCESS:
            _payment_success(db, data)
        elif key == ev.SUBSCRIPTION_EXPIRED:
            _subscription_expired(db, data)
        elif key in (ev.COURSE_UPSERTED, ev.COURSE_DELETED):
            _course_changed(db, key, data)
        else:
            replicas.apply(db, key, data, user=ReplicaUser, stream=ReplicaStream)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def start_consumer() -> None:
    messaging.start_consumer("enrollment", QUEUE, ev.QUEUES[QUEUE], handle)
