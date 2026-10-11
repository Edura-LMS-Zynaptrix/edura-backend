import os
from typing import Tuple

import httpx
from fastapi import HTTPException

CONTENT_SERVICE_URL = os.getenv("CONTENT_SERVICE_URL", "http://content_service:8004")


async def upload_receipt(content: bytes, filename: str) -> Tuple[str, str]:
    """Uploads receipt bytes to Cloudinary via content_service's internal
    endpoint (content_service is the sole owner of the Cloudinary
    integration/credentials). Returns (secure_url, public_id) — the
    public_id is stored too so the asset can be managed/deleted later,
    not just linked to."""
    url = f"{CONTENT_SERVICE_URL}/internal/receipt-upload"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, files={"file": (filename, content)})
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "RECEIPT_UPLOAD_UNREACHABLE",
                "message": f"Could not reach content_service to upload the receipt: {e}",
            },
        )

    if r.status_code != 200:
        raise HTTPException(
            status_code=r.status_code, detail=r.json().get("detail", r.text)
        )

    data = r.json()
    return data["secure_url"], data["public_id"]
