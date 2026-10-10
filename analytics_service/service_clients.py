import logging
import os

import httpx

logger = logging.getLogger(__name__)

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user_service:8002")
COURSE_SERVICE_URL = os.getenv("COURSE_SERVICE_URL", "http://course_service:8003")
ENROLLMENT_SERVICE_URL = os.getenv("ENROLLMENT_SERVICE_URL", "http://enrollment_service:8005")
ASSESSMENT_SERVICE_URL = os.getenv("ASSESSMENT_SERVICE_URL", "http://assessment_service:8007")


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
        enrollments = await _get(client, f"{ENROLLMENT_SERVICE_URL}/internal/course-counts", [])
        assessments = await _get(client, f"{ASSESSMENT_SERVICE_URL}/internal/stats", {})
    return {
        "users": users,
        "courses": courses,
        "enrollments": enrollments,
        "assessments": assessments,
    }
