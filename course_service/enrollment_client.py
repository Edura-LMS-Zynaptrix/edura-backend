import os

import httpx

ENROLLMENT_SERVICE_URL = os.getenv(
    "ENROLLMENT_SERVICE_URL", "http://enrollment_service:8005"
)


async def is_enrolled(
    student_id: int, course_id: int, module_id: int | None = None
) -> bool:
    """Active course enrollment, or (when module_id is given) an admin grant for that one lesson."""
    params = {"student_id": student_id, "course_id": course_id}
    if module_id is not None:
        params["module_id"] = module_id
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{ENROLLMENT_SERVICE_URL}/internal/access", params=params
            )
        return r.status_code == 200 and bool(r.json().get("has_access"))
    except Exception:
        return False
