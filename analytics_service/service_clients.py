import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth_service:8001")
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user_service:8002")
COURSE_SERVICE_URL = os.getenv("COURSE_SERVICE_URL", "http://course_service:8003")
ENROLLMENT_SERVICE_URL = os.getenv(
    "ENROLLMENT_SERVICE_URL", "http://enrollment_service:8005"
)
ASSESSMENT_SERVICE_URL = os.getenv(
    "ASSESSMENT_SERVICE_URL", "http://assessment_service:8007"
)
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment_service:8006")

# The serverless database behind the services can take several seconds to wake.
_TIMEOUT = 10.0


class SourceUnavailable(Exception):
    """A service whose data a view depends on could not be reached."""


async def _get(client: httpx.AsyncClient, url: str, default):
    """One failing service shouldn't blank the whole homepage — fall back and log."""
    try:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error("Stats source unavailable (%s): %s", url, e)
        return default


async def fetch_platform_sources() -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        users = await _get(client, f"{USER_SERVICE_URL}/internal/stats", {})
        courses = await _get(client, f"{COURSE_SERVICE_URL}/internal/published", [])
        enrollments = await _get(
            client, f"{ENROLLMENT_SERVICE_URL}/internal/course-counts", []
        )
        assessments = await _get(client, f"{ASSESSMENT_SERVICE_URL}/internal/stats", {})
    return {
        "users": users,
        "courses": courses,
        "enrollments": enrollments,
        "assessments": assessments,
    }


async def _required(url: str, params: dict | None = None):
    """For reports, a missing source would show up as false zeros — fail loudly instead.

    Returns None for 404 so callers can tell "no such record" from "service down".
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, params=params)
    except Exception as e:
        logger.error("Progress source unreachable (%s): %r", url, e)
        raise SourceUnavailable(url) from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        logger.error("Progress source failed (%s): HTTP %s", url, r.status_code)
        raise SourceUnavailable(url)
    return r.json()


def _ids(values) -> str:
    return ",".join(str(v) for v in sorted(set(values)))


async def profile(user_id: int) -> dict | None:
    return await _required(f"{USER_SERVICE_URL}/internal/profiles/{user_id}")


async def account(user_id: int) -> dict | None:
    return await _required(f"{AUTH_SERVICE_URL}/internal/users/{user_id}")


async def students(limit: int = 500) -> list[dict]:
    return (
        await _required(f"{USER_SERVICE_URL}/internal/students", {"limit": limit}) or []
    )


async def accessible(user_id: int) -> dict:
    data = await _required(
        f"{ENROLLMENT_SERVICE_URL}/internal/students/{user_id}/accessible"
    )
    return data or {"course_ids": [], "lesson_ids": []}


async def exam_progress(
    user_id: int, course_ids: list[int], lesson_ids: list[int]
) -> list[dict]:
    if not course_ids and not lesson_ids:
        return []
    params = {}
    if course_ids:
        params["course_ids"] = _ids(course_ids)
    if lesson_ids:
        params["lesson_ids"] = _ids(lesson_ids)
    return (
        await _required(
            f"{ASSESSMENT_SERVICE_URL}/internal/students/{user_id}/exam-progress",
            params,
        )
        or []
    )


async def badge_count(user_id: int) -> int:
    data = await _required(
        f"{ASSESSMENT_SERVICE_URL}/internal/students/{user_id}/badge-count"
    )
    return int((data or {}).get("count", 0))


async def course_titles(course_ids) -> dict[int, str]:
    if not course_ids:
        return {}
    data = await _required(
        f"{COURSE_SERVICE_URL}/internal/titles", {"ids": _ids(course_ids)}
    )
    return {int(k): v for k, v in (data or {}).items()}


async def course_modules(course_id: int) -> list[dict] | None:
    return await _required(f"{COURSE_SERVICE_URL}/internal/courses/{course_id}/modules")


async def first_attempts(course_id: int) -> dict:
    data = await _required(
        f"{ASSESSMENT_SERVICE_URL}/internal/courses/{course_id}/first-attempts"
    )
    return data or {"exams": [], "attempts": []}


async def names(user_ids) -> dict[int, str]:
    if not user_ids:
        return {}
    data = await _required(
        f"{USER_SERVICE_URL}/internal/names", {"ids": _ids(user_ids)}
    )
    return {int(k): v for k, v in (data or {}).items()}


async def course_info(course_id: int) -> dict | None:
    return await _required(f"{COURSE_SERVICE_URL}/internal/courses/{course_id}")


async def admin_sources() -> dict:
    """Everything the admin dashboard and analytics page are built from, fetched together."""
    users, courses, enrollments, assessments, payments = await asyncio.gather(
        _required(f"{USER_SERVICE_URL}/internal/student-breakdown"),
        _required(f"{COURSE_SERVICE_URL}/internal/published"),
        _required(f"{ENROLLMENT_SERVICE_URL}/internal/course-counts"),
        _required(f"{ASSESSMENT_SERVICE_URL}/internal/admin-stats"),
        _required(f"{PAYMENT_SERVICE_URL}/internal/admin-stats"),
    )
    return {
        "users": users or {},
        "courses": courses or [],
        "enrollments": enrollments or [],
        "assessments": assessments or {},
        "payments": payments or {},
    }
