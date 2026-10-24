"""payment_service's queue: the catalogue, so a price is checked against what the course really costs."""

from database import SessionLocal
from models import ReplicaCourse

from shared import events as ev
from shared import messaging, replicas

QUEUE = "payment.queue"


def handle(key: str, data: dict) -> None:
    db = SessionLocal()
    try:
        replicas.apply(db, key, data, course=ReplicaCourse)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def start() -> None:
    messaging.start_consumer("payment", QUEUE, ev.QUEUES[QUEUE], handle)
