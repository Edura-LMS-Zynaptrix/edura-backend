"""Display names for the chat inbox, from the local copy of users (``replica_users``).

user_service and auth_service announce every profile and account change; the
event consumer keeps this copy current, so nothing here calls another service.
"""

from database import SessionLocal
from fastapi.concurrency import run_in_threadpool
from models import ReplicaUser


def _names(user_ids: list[int]) -> dict[int, str]:
    db = SessionLocal()
    try:
        rows = db.query(ReplicaUser).filter(ReplicaUser.user_id.in_(user_ids)).all()
        return {r.user_id: r.display_name for r in rows if r.display_name}
    finally:
        db.close()


async def display_names(user_ids: list[int]) -> dict[int, str]:
    """Profile name, falling back to the account email; unknown ids are absent."""
    ids = sorted({i for i in user_ids if i is not None})
    if not ids:
        return {}
    return await run_in_threadpool(_names, ids)
