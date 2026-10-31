"""assessment_service's queue: access, catalogue, and people (the copies it keeps)."""

from database import SessionLocal
from models import ReplicaAccess, ReplicaCourse, ReplicaUser

from shared import events as ev
from shared import messaging, replicas

QUEUE = "assessment.queue"


def handle(key: str, data: dict) -> None:
    db = SessionLocal()
    try:
        replicas.apply(
            db,
            key,
            data,
            access=ReplicaAccess,
            course=ReplicaCourse,
            user=ReplicaUser,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def start() -> None:
    messaging.start_consumer("assessment", QUEUE, ev.QUEUES[QUEUE], handle)
