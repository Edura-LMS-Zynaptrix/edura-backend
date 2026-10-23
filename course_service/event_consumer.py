"""course_service's queue: the access list, so paid content can be gated locally."""

from database import SessionLocal
from models import ReplicaAccess

from shared import events as ev
from shared import messaging, replicas

QUEUE = "course.queue"


def handle(key: str, data: dict) -> None:
    db = SessionLocal()
    try:
        replicas.apply(db, key, data, access=ReplicaAccess)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def start() -> None:
    messaging.start_consumer("course", QUEUE, ev.QUEUES[QUEUE], handle)
