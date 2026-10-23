"""admin_service's queue: people, so the chat inbox can show names."""

from database import SessionLocal
from models import ReplicaUser

from shared import events as ev
from shared import messaging, replicas

QUEUE = "admin.queue"


def handle(key: str, data: dict) -> None:
    db = SessionLocal()
    try:
        replicas.apply(db, key, data, user=ReplicaUser)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def start() -> None:
    messaging.start_consumer("admin", QUEUE, ev.QUEUES[QUEUE], handle)
