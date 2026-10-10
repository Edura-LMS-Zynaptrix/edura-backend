import os
import time

import cloudinary
import cloudinary.uploader
import cloudinary.utils
from fastapi import HTTPException

FOLDER = "edura/uploads"
AVATAR_FOLDER = "edura/avatars"
RECEIPT_FOLDER = "edura/receipts"


def _configure():
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key = os.getenv("CLOUDINARY_API_KEY", "")
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "")
    if not (cloud_name and api_key and api_secret):
        raise HTTPException(
            status_code=503,
            detail={
                "error": "CLOUDINARY_NOT_CONFIGURED",
                "message": "Cloudinary credentials are not set",
            },
        )
    cloudinary.config(
        cloud_name=cloud_name, api_key=api_key, api_secret=api_secret, secure=True
    )


def upload_asset(
    content: bytes,
    filename: str,
    resource_type: str = "auto",
    folder: str = FOLDER,
) -> tuple[str, str]:
    """Upload bytes to Cloudinary. Returns (public_id, secure_url)."""
    _configure()
    result = cloudinary.uploader.upload(
        content,
        folder=folder,
        use_filename=True,
        unique_filename=True,
        filename_override=filename,
        resource_type=resource_type,
        overwrite=False,
    )
    return result["public_id"], result["secure_url"]


def delete_asset(public_id: str) -> bool:
    """Delete an asset whatever its resource type. True if something was removed."""
    _configure()
    for resource_type in ("image", "raw", "video"):
        result = cloudinary.uploader.destroy(
            public_id, resource_type=resource_type, invalidate=True
        )
        if result.get("result") == "ok":
            return True
    return False


def get_signed_url(public_id: str) -> tuple[str, int]:
    """Generate a 1-hour signed URL for an existing Cloudinary asset."""
    _configure()
    expires_at = int(time.time()) + 3600
    url, _ = cloudinary.utils.cloudinary_url(
        public_id,
        sign_url=True,
        type="upload",
        expires_at=expires_at,
    )
    return url, expires_at
