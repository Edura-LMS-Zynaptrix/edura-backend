import os

import httpx

ENROLLMENT_SERVICE_URL = os.getenv(
    "ENROLLMENT_SERVICE_URL", "http://enrollment_service:8005"
)


async def is_enrolled(student_id: int, course_id: int) -> bool:
    url = f"{ENROLLMENT_SERVICE_URL}/enrollments"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                url, params={"student_id": student_id, "course_id": course_id}
            )
        if r.status_code != 200:
            return False
        data = r.json()
        if isinstance(data, dict):
            status = str(data.get("status", "")).upper()
            if status == "ACTIVE":
                return True
            items = data.get("items", [])
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return any(str(e.get("status", "")).upper() == "ACTIVE" for e in items)
    except Exception:
        return False
