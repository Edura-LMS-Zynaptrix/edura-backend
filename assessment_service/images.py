"""Storing question and exam images straight in Cloudinary (no other service is involved)."""

from fastapi.concurrency import run_in_threadpool

from shared import media

ALLOWED = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_BYTES = 10 * 1024 * 1024


async def upload_image(data: bytes, filename: str) -> tuple[str, str]:
    """Returns (secure_url, public_id). Raises ValueError with a readable reason."""
    try:
        media.check(data, ALLOWED, MAX_BYTES, "Image")
        public_id, url = await run_in_threadpool(
            media.upload, data, filename, media.UPLOAD_FOLDER, "image"
        )
    except media.MediaError as e:
        raise ValueError(e.message) from e
    return url, public_id
