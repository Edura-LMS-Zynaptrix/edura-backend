"""Course owner and titles, read from the local copy of the catalogue (``replica_courses``)."""

from database import SessionLocal
from fastapi.concurrency import run_in_threadpool
from models import ReplicaCourse


def _instructor(course_id: int) -> int | None:
    db = SessionLocal()
    try:
        course = db.get(ReplicaCourse, course_id)
        return course.instructor_id if course else None
    finally:
        db.close()


def _titles(course_ids: list[int]) -> dict[int, str]:
    db = SessionLocal()
    try:
        rows = db.query(ReplicaCourse).filter(ReplicaCourse.id.in_(course_ids)).all()
        return {r.id: r.title for r in rows}
    finally:
        db.close()


async def get_course_instructor_id(course_id: int) -> int | None:
    """Who owns the course, so a teacher can only manage their own exams."""
    return await run_in_threadpool(_instructor, course_id)


async def get_course_titles(course_ids: list[int]) -> dict[int, str]:
    """{course_id: title} for display; unknown ids are simply absent."""
    if not course_ids:
        return {}
    return await run_in_threadpool(_titles, sorted(set(course_ids)))
