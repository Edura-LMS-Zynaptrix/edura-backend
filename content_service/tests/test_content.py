"""
Tests for content_service: signed URL, stream, and upload endpoints.
All external calls (Cloudinary, enrollment_service, python-magic) are mocked.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import jwt

os.environ.setdefault("SECRET_KEY", "test-secret-for-content-service")

from main import app  # noqa: E402

SECRET = os.environ["SECRET_KEY"]
ALGORITHM = "HS256"

client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(user_id: int, role: str) -> str:
    return jwt.encode(
        {
            "sub": str(user_id),
            "role": role,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        SECRET,
        algorithm=ALGORITHM,
    )


def _auth(user_id: int, role: str) -> dict:
    return {"Authorization": f"Bearer {_make_token(user_id, role)}"}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_ok(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["service"] == "content_service"


# ---------------------------------------------------------------------------
# POST /signed-url
# ---------------------------------------------------------------------------


class TestSignedUrl:
    def test_missing_token_returns_401(self):
        r = client.post(
            "/signed-url", json={"public_id": "img/test.jpg", "course_id": 1}
        )
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "MISSING_TOKEN"

    def test_student_not_enrolled_returns_403(self):
        with patch("router.is_enrolled", new=AsyncMock(return_value=False)):
            r = client.post(
                "/signed-url",
                json={"public_id": "img/test.jpg", "course_id": 1},
                headers=_auth(5, "student"),
            )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "NOT_ENROLLED"

    def test_enrolled_student_gets_signed_url(self):
        with (
            patch("router.is_enrolled", new=AsyncMock(return_value=True)),
            patch(
                "router.get_signed_url",
                return_value=("https://res.cloudinary.com/signed?sig=abc", 9999999999),
            ),
        ):
            r = client.post(
                "/signed-url",
                json={"public_id": "img/test.jpg", "course_id": 1},
                headers=_auth(5, "student"),
            )
        assert r.status_code == 200
        data = r.json()
        assert "signed_url" in data
        assert "expires_at" in data

    def test_teacher_gets_signed_url_without_enrollment_check(self):
        with patch(
            "router.get_signed_url",
            return_value=("https://res.cloudinary.com/signed?sig=xyz", 9999999999),
        ):
            r = client.post(
                "/signed-url",
                json={"public_id": "img/lecture.jpg", "course_id": 1},
                headers=_auth(10, "teacher"),
            )
        assert r.status_code == 200
        assert "signed_url" in r.json()

    def test_admin_gets_signed_url_without_enrollment_check(self):
        with patch(
            "router.get_signed_url",
            return_value=("https://res.cloudinary.com/signed?sig=adm", 9999999999),
        ):
            r = client.post(
                "/signed-url",
                json={"public_id": "img/admin.jpg", "course_id": 1},
                headers=_auth(1, "admin"),
            )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# GET /lessons/{lesson_id}/stream
# ---------------------------------------------------------------------------


class TestStreamLesson:
    def test_missing_token_returns_401(self):
        r = client.get("/lessons/1/stream?youtube_video_id=dQw4w9WgXcQ")
        assert r.status_code == 401

    def test_student_without_course_id_returns_422(self):
        r = client.get(
            "/lessons/1/stream?youtube_video_id=dQw4w9WgXcQ",
            headers=_auth(5, "student"),
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "COURSE_ID_REQUIRED"

    def test_student_not_enrolled_returns_403(self):
        with patch("router.is_enrolled", new=AsyncMock(return_value=False)):
            r = client.get(
                "/lessons/1/stream?youtube_video_id=dQw4w9WgXcQ&course_id=1",
                headers=_auth(5, "student"),
            )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "NOT_ENROLLED"

    def test_enrolled_student_gets_embed_url(self):
        with patch("router.is_enrolled", new=AsyncMock(return_value=True)):
            r = client.get(
                "/lessons/1/stream?youtube_video_id=dQw4w9WgXcQ&course_id=1",
                headers=_auth(5, "student"),
            )
        assert r.status_code == 200
        data = r.json()
        assert "dQw4w9WgXcQ" in data["embed_url"]
        assert "youtube-nocookie.com" in data["embed_url"]
        assert "rel=0" in data["embed_url"]
        assert "modestbranding=1" in data["embed_url"]
        assert data["allow_origin"] == "https://edura.lk"

    def test_teacher_gets_embed_url_without_enrollment(self):
        r = client.get(
            "/lessons/1/stream?youtube_video_id=abc123",
            headers=_auth(10, "teacher"),
        )
        assert r.status_code == 200
        data = r.json()
        assert "abc123" in data["embed_url"]
        assert data["allow_origin"] == "https://edura.lk"


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------


class TestUpload:
    def test_student_cannot_upload(self):
        r = client.post(
            "/upload",
            headers=_auth(5, "student"),
            files={"file": ("test.pdf", b"data", "application/pdf")},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"

    def test_missing_token_returns_401(self):
        r = client.post(
            "/upload", files={"file": ("test.pdf", b"data", "application/pdf")}
        )
        assert r.status_code == 401

    def test_file_too_large_returns_413(self):
        big_content = b"x" * (25 * 1024 * 1024 + 1)
        r = client.post(
            "/upload",
            headers=_auth(10, "teacher"),
            files={"file": ("big.pdf", big_content, "application/pdf")},
        )
        assert r.status_code == 413
        assert r.json()["detail"]["error"] == "FILE_TOO_LARGE"

    def test_disallowed_mime_type_returns_415(self):
        r = client.post(
            "/upload",
            headers=_auth(10, "teacher"),
            files={"file": ("script.js", b"console.log(1)", "application/javascript")},
        )
        assert r.status_code == 415
        assert r.json()["detail"]["error"] == "UNSUPPORTED_MEDIA_TYPE"

    def test_mime_mismatch_returns_415(self):
        with patch("router.magic.from_buffer", return_value="application/x-executable"):
            r = client.post(
                "/upload",
                headers=_auth(10, "teacher"),
                files={"file": ("fake.pdf", b"%PDF-1.4", "application/pdf")},
            )
        assert r.status_code == 415
        assert r.json()["detail"]["error"] == "MIME_MISMATCH"

    def test_valid_pdf_upload_passes(self):
        pdf_bytes = b"%PDF-1.4 fake pdf content"
        with (
            patch("router.magic.from_buffer", return_value="application/pdf"),
            patch(
                "router.upload_asset",
                return_value=(
                    "edura/uploads/lecture",
                    "https://res.cloudinary.com/demo/raw/upload/edura/uploads/lecture.pdf",
                ),
            ),
        ):
            r = client.post(
                "/upload",
                headers=_auth(10, "teacher"),
                files={"file": ("lecture.pdf", pdf_bytes, "application/pdf")},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["valid"] is True
        assert data["mime_type"] == "application/pdf"
        assert data["filename"] == "lecture.pdf"
        assert data["public_id"] == "edura/uploads/lecture"
        assert "cloudinary.com" in data["secure_url"]

    def test_valid_png_upload_passes(self):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        with (
            patch("router.magic.from_buffer", return_value="image/png"),
            patch(
                "router.upload_asset",
                return_value=(
                    "edura/uploads/thumb",
                    "https://res.cloudinary.com/demo/image/upload/edura/uploads/thumb.png",
                ),
            ),
        ):
            r = client.post(
                "/upload",
                headers=_auth(10, "teacher"),
                files={"file": ("thumb.png", png_bytes, "image/png")},
            )
        assert r.status_code == 200
        assert r.json()["mime_type"] == "image/png"
        assert "public_id" in r.json()

    def test_admin_can_upload(self):
        jpeg_bytes = b"\xff\xd8\xff" + b"\x00" * 50
        with (
            patch("router.magic.from_buffer", return_value="image/jpeg"),
            patch(
                "router.upload_asset",
                return_value=(
                    "edura/uploads/photo",
                    "https://res.cloudinary.com/demo/image/upload/edura/uploads/photo.jpg",
                ),
            ),
        ):
            r = client.post(
                "/upload",
                headers=_auth(1, "admin"),
                files={"file": ("photo.jpg", jpeg_bytes, "image/jpeg")},
            )
        assert r.status_code == 200
        assert r.json()["valid"] is True
        assert r.json()["public_id"] == "edura/uploads/photo"
