import logging
import os

import httpx

logger = logging.getLogger(__name__)

CONTENT_SERVICE_URL = os.getenv("CONTENT_SERVICE_URL", "http://content_service:8004")


async def upload_image(data: bytes, filename: str) -> tuple[str, str]:
    """Store an image via content_service (which owns Cloudinary). Returns (secure_url, public_id)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{CONTENT_SERVICE_URL}/internal/image-upload",
            files={"file": (filename, data, "application/octet-stream")},
        )
    if r.status_code != 200:
        detail = (
            r.json().get("detail", {})
            if r.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        raise ValueError(detail.get("message") or f"upload failed ({r.status_code})")
    body = r.json()
    return body["secure_url"], body["public_id"]
