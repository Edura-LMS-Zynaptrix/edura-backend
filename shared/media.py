"""Cloudinary uploads for services that store a user's file next to their own data.

Payment stores receipts and assessment stores question images. Neither asks
another service to do it: each uploads straight to Cloudinary with this helper
and keeps the returned ``public_id`` (and URL) in its own table.

The file's own signature decides its type, never its name or the declared MIME.
"""

import os

RECEIPT_FOLDER = "edura/receipts"
UPLOAD_FOLDER = "edura/uploads"


class MediaError(Exception):
    """A file was refused or could not be stored. Carries an HTTP-style status."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def detail(self) -> dict:
        return {"error": self.code, "message": self.message}


def detect_type(content: bytes) -> str | None:
    """The MIME type of the bytes, from their signature."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def check(content: bytes, allowed: set[str], max_bytes: int, what: str = "File") -> str:
    """Raise MediaError unless the bytes are non-empty, small enough and of an allowed type."""
    if not content:
        raise MediaError(422, "EMPTY_FILE", f"{what} is empty")
    if len(content) > max_bytes:
        raise MediaError(
            413, "FILE_TOO_LARGE", f"{what} exceeds {max_bytes // (1024 * 1024)} MB"
        )
    detected = detect_type(content)
    if detected not in allowed:
        raise MediaError(
            415,
            "UNSUPPORTED_FILE_TYPE",
            f"{what} content is '{detected or 'unknown'}', which is not allowed",
        )
    return detected


def upload(
    content: bytes, filename: str, folder: str, resource_type: str = "auto"
) -> tuple[str, str]:
    """Upload bytes to Cloudinary. Returns (public_id, secure_url)."""
    import cloudinary
    import cloudinary.uploader

    cloud = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    key = os.getenv("CLOUDINARY_API_KEY", "")
    secret = os.getenv("CLOUDINARY_API_SECRET", "")
    if not (cloud and key and secret):
        raise MediaError(
            503, "CLOUDINARY_NOT_CONFIGURED", "Cloudinary credentials are not set"
        )
    cloudinary.config(cloud_name=cloud, api_key=key, api_secret=secret, secure=True)
    try:
        result = cloudinary.uploader.upload(
            content,
            folder=folder,
            use_filename=True,
            unique_filename=True,
            filename_override=filename,
            resource_type=resource_type,
            overwrite=False,
        )
    except Exception as exc:
        raise MediaError(
            502, "UPLOAD_FAILED", f"Could not store the file: {exc}"
        ) from exc
    return result["public_id"], result["secure_url"]
