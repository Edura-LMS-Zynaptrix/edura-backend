"""Who may stream or sign paid content, answered from this service's own copy of the access list.

enrollment_service announces what each student can open (``access.changed``);
``event_consumer`` stores it in ``replica_access``. Nothing here calls another service.
"""

from database import SessionLocal
from fastapi.concurrency import run_in_threadpool
from models import ReplicaAccess

from shared.replicas import has_access


def _check(student_id: int, course_id: int) -> bool:
    db = SessionLocal()
    try:
        return has_access(db, ReplicaAccess, student_id, course_id)
    finally:
        db.close()


async def is_enrolled(student_id: int, course_id: int) -> bool:
    """True when the student may open the whole course."""
    return await run_in_threadpool(_check, student_id, course_id)
