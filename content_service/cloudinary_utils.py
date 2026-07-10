import os
import time

import cloudinary
import cloudinary.uploader
import cloudinary.utils
from fastapi import HTTPException

FOLDER = "edura/uploads"


def _configure():
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key = os.getenv("CLOUDINARY_API_KEY", "")
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "")
    if not (cloud_name and api_key and api_secret):
        raise HTTPException(
            status_code=503,
            detail={"error": "CLOUDINARY_NOT_CONFIGURED", "message": "Cloudinary credentials are not set"},
        )
    cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret, secure=True)


def upload_asset(content: bytes, filename: str) -> tuple[str, str]:
    """Upload bytes to Cloudinary. Returns (public_id, secure_url)."""
    _configure()
    result = cloudinary.uploader.upload(
        content,
        folder=FOLDER,
        use_filename=True,
        unique_filename=True,
        resource_type="auto",
        overwrite=False,
    )
    return result["public_id"], result["secure_url"]


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
