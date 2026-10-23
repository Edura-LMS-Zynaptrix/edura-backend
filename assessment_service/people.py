"""Names, schools and districts for leaderboards, from the local copy of users (``replica_users``)."""

from database import SessionLocal
from fastapi.concurrency import run_in_threadpool
from models import ReplicaUser


def _profiles(user_ids: list[int]) -> dict[int, dict]:
    db = SessionLocal()
    try:
        rows = db.query(ReplicaUser).filter(ReplicaUser.user_id.in_(user_ids)).all()
        return {
            r.user_id: {
                "user_id": r.user_id,
                "role": r.role or "",
                "full_name": r.full_name or "",
                "school": r.school,
                "district": r.district,
                "grade": r.grade,
            }
            for r in rows
        }
    finally:
        db.close()


async def get_profiles(user_ids: list[int]) -> dict[int, dict]:
    if not user_ids:
        return {}
    return await run_in_threadpool(_profiles, sorted(set(user_ids)))
