import os

import httpx

ENROLLMENT_SERVICE_URL = os.getenv(
    "ENROLLMENT_SERVICE_URL", "http://enrollment_service:8005"
)


async def is_enrolled(student_id: int, course_id: int) -> bool:
    """enrollment_service returns one enrollment object (404 when none)."""
    url = f"{ENROLLMENT_SERVICE_URL}/enrollments"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                url, params={"student_id": student_id, "course_id": course_id}
            )
        if r.status_code != 200:
            return False
        return str(r.json().get("status", "")).upper() == "ACTIVE"
    except Exception:
        return False
