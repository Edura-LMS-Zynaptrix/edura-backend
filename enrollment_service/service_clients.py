import logging
import os

import httpx

logger = logging.getLogger(__name__)

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user_service:8002")
COURSE_SERVICE_URL = os.getenv("COURSE_SERVICE_URL", "http://course_service:8003")


def allowed_student_grades(course_grade: int | None) -> list[int] | None:
    """Which students may see a course: the reverse of the O/L (10<->11) and A/L (12<->13) bands."""
    if course_grade is None:
        return None
    if course_grade == 10:
        return [10, 11]
    if course_grade == 12:
        return [12, 13]
    return [course_grade]


async def course_info(course_id: int) -> dict | None:
    # One retry: the first query after the serverless database has idled can be slow.
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{COURSE_SERVICE_URL}/internal/courses/{course_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning("Course lookup for %s failed (attempt %s): %r", course_id, attempt, e)
    return None


async def students(grades: list[int] | None, search: str | None, limit: int = 100) -> list[dict]:
    params: dict = {"limit": limit}
    if grades:
        params["grades"] = ",".join(map(str, grades))
    if search:
        params["search"] = search
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{USER_SERVICE_URL}/internal/students", params=params)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.error("Student lookup failed: %s", e)
        return []


async def names(user_ids: list[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{USER_SERVICE_URL}/internal/names",
                params={"ids": ",".join(map(str, sorted(set(user_ids))))},
            )
        return {int(k): v for k, v in r.json().items()} if r.status_code == 200 else {}
    except Exception as e:
        logger.error("Name lookup failed: %s", e)
        return {}


async def streams() -> dict[int, str]:
    """Active A/L streams {id: name} from user_service (public endpoint)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{USER_SERVICE_URL}/streams", params={"active_only": "true"})
        return {int(row["id"]): row["name"] for row in r.json()} if r.status_code == 200 else {}
    except Exception as e:
        logger.error("Stream lookup failed: %s", e)
        return {}

