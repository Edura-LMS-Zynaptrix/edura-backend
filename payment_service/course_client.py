import os
from typing import Optional

import httpx
from fastapi import HTTPException

COURSE_SERVICE_URL = os.getenv("COURSE_SERVICE_URL", "http://course_service:8003")


async def get_course_price(course_id: int) -> Optional[float]:
    """Server-side price lookup — free-enrollment must never trust a
    client-supplied price, only what course_service actually has on record."""
    url = f"{COURSE_SERVICE_URL}/{course_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={"error": "COURSE_SERVICE_UNREACHABLE", "message": str(e)},
        )

    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise HTTPException(
            status_code=r.status_code, detail=r.json().get("detail", r.text)
        )

    return float(r.json()["price"])
