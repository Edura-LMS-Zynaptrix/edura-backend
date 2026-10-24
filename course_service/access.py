"""Who may open paid content, answered from this service's own copy of the access list.

enrollment_service announces what each student can open (``access.changed``);
``event_consumer`` stores it in ``replica_access``. Nothing here calls another service.
"""

from database import SessionLocal
from fastapi.concurrency import run_in_threadpool
from models import ReplicaAccess

from shared.replicas import has_access


def _check(student_id: int, course_id: int, module_id: int | None) -> bool:
    db = SessionLocal()
    try:
        return has_access(db, ReplicaAccess, student_id, course_id, module_id)
    finally:
        db.close()


async def is_enrolled(
    student_id: int, course_id: int, module_id: int | None = None
) -> bool:
    """A whole-course grant, or (with module_id) a grant for just that lesson."""
    return await run_in_threadpool(_check, student_id, course_id, module_id)
