"""
Tests for content_service: signed URL, stream, and upload endpoints.
All external calls (Cloudinary, enrollment_service, python-magic) are mocked.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import jwt
from fastapi.testclient import TestClient

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
    def test_given_running_service_when_health_requested_then_200(self):
        # Act
        r = client.get("/health")

        # Assert
        assert r.status_code == 200
        assert r.json()["service"] == "content_service"


# ---------------------------------------------------------------------------
# POST /signed-url
# ---------------------------------------------------------------------------


class TestSignedUrl:
    def test_given_no_token_when_signed_url_requested_then_401_missing_token(self):
        # Act
        r = client.post(
            "/signed-url", json={"public_id": "img/test.jpg", "course_id": 1}
        )

        # Assert
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "MISSING_TOKEN"

    def test_given_student_not_enrolled_when_signed_url_requested_then_403_not_enrolled(
        self,
    ):
        # Act
        with patch("router.is_enrolled", new=AsyncMock(return_value=False)):
            r = client.post(
                "/signed-url",
                json={"public_id": "img/test.jpg", "course_id": 1},
                headers=_auth(5, "student"),
            )

        # Assert
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "NOT_ENROLLED"

    def test_given_enrolled_student_when_signed_url_requested_then_signed_url_returned(
        self,
    ):
        # Act
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

        # Assert
        assert r.status_code == 200

        # Act
        data = r.json()

        # Assert
        assert "signed_url" in data
        assert "expires_at" in data

    def test_given_teacher_when_signed_url_requested_then_url_returned_without_enrollment_check(
        self,
    ):
        # Act
        with patch(
            "router.get_signed_url",
            return_value=("https://res.cloudinary.com/signed?sig=xyz", 9999999999),
        ):
            r = client.post(
                "/signed-url",
                json={"public_id": "img/lecture.jpg", "course_id": 1},
                headers=_auth(10, "teacher"),
            )

        # Assert
        assert r.status_code == 200
        assert "signed_url" in r.json()

    def test_given_admin_when_signed_url_requested_then_url_returned_without_enrollment_check(
        self,
    ):
        # Act
        with patch(
            "router.get_signed_url",
            return_value=("https://res.cloudinary.com/signed?sig=adm", 9999999999),
        ):
            r = client.post(
                "/signed-url",
                json={"public_id": "img/admin.jpg", "course_id": 1},
                headers=_auth(1, "admin"),
            )

        # Assert
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# GET /lessons/{lesson_id}/stream
# ---------------------------------------------------------------------------


class TestStreamLesson:
    def test_given_no_token_when_lesson_stream_requested_then_401(self):
        # Act
        r = client.get("/lessons/1/stream?youtube_video_id=dQw4w9WgXcQ")

        # Assert
        assert r.status_code == 401

    def test_given_student_without_course_id_when_lesson_stream_requested_then_422_course_id_required(
        self,
    ):
        # Act
        r = client.get(
            "/lessons/1/stream?youtube_video_id=dQw4w9WgXcQ",
            headers=_auth(5, "student"),
        )

        # Assert
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "COURSE_ID_REQUIRED"

    def test_given_student_not_enrolled_when_lesson_stream_requested_then_403_not_enrolled(
        self,
    ):
        # Act
        with patch("router.is_enrolled", new=AsyncMock(return_value=False)):
            r = client.get(
                "/lessons/1/stream?youtube_video_id=dQw4w9WgXcQ&course_id=1",
                headers=_auth(5, "student"),
            )

        # Assert
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "NOT_ENROLLED"

    def test_given_enrolled_student_when_lesson_stream_requested_then_embed_url_returned(
        self,
    ):
        # Act
        with patch("router.is_enrolled", new=AsyncMock(return_value=True)):
            r = client.get(
                "/lessons/1/stream?youtube_video_id=dQw4w9WgXcQ&course_id=1",
                headers=_auth(5, "student"),
            )

        # Assert
        assert r.status_code == 200

        # Act
        data = r.json()

        # Assert
        assert "dQw4w9WgXcQ" in data["embed_url"]
        assert "youtube-nocookie.com" in data["embed_url"]
        assert "rel=0" in data["embed_url"]
        assert "modestbranding=1" in data["embed_url"]
        assert data["allow_origin"] == "https://edura.lk"

    def test_given_teacher_when_lesson_stream_requested_then_embed_url_returned_without_enrollment(
        self,
    ):
        # Act
        r = client.get(
            "/lessons/1/stream?youtube_video_id=abc123",
            headers=_auth(10, "teacher"),
        )

        # Assert
        assert r.status_code == 200

        # Act
        data = r.json()

        # Assert
        assert "abc123" in data["embed_url"]
        assert data["allow_origin"] == "https://edura.lk"


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------


class TestUpload:
    def test_given_student_when_uploading_then_403_insufficient_permissions(self):
        # Act
        r = client.post(
            "/upload",
            headers=_auth(5, "student"),
            files={"file": ("test.pdf", b"data", "application/pdf")},
        )

        # Assert
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"

    def test_given_no_token_when_uploading_then_401(self):
        # Act
        r = client.post(
            "/upload", files={"file": ("test.pdf", b"data", "application/pdf")}
        )

        # Assert
        assert r.status_code == 401

    def test_given_oversized_file_when_uploading_then_413_file_too_large(self):
        # Arrange
        big_content = b"x" * (25 * 1024 * 1024 + 1)

        # Act
        r = client.post(
            "/upload",
            headers=_auth(10, "teacher"),
            files={"file": ("big.pdf", big_content, "application/pdf")},
        )

        # Assert
        assert r.status_code == 413
        assert r.json()["detail"]["error"] == "FILE_TOO_LARGE"

    def test_given_disallowed_mime_type_when_uploading_then_415_unsupported_media_type(
        self,
    ):
        # Act
        r = client.post(
            "/upload",
            headers=_auth(10, "teacher"),
            files={"file": ("script.js", b"console.log(1)", "application/javascript")},
        )

        # Assert
        assert r.status_code == 415
        assert r.json()["detail"]["error"] == "UNSUPPORTED_MEDIA_TYPE"

    def test_given_content_not_matching_declared_type_when_uploading_then_415_mime_mismatch(
        self,
    ):
        # Act
        with patch("router.magic.from_buffer", return_value="application/x-executable"):
            r = client.post(
                "/upload",
                headers=_auth(10, "teacher"),
                files={"file": ("fake.pdf", b"%PDF-1.4", "application/pdf")},
            )

        # Assert
        assert r.status_code == 415
        assert r.json()["detail"]["error"] == "MIME_MISMATCH"

    def test_given_valid_pdf_when_uploading_then_200(self):
        # Arrange
        pdf_bytes = b"%PDF-1.4 fake pdf content"

        # Act
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

        # Assert
        assert r.status_code == 200

        # Act
        data = r.json()

        # Assert
        assert data["valid"] is True
        assert data["mime_type"] == "application/pdf"
        assert data["filename"] == "lecture.pdf"
        assert data["public_id"] == "edura/uploads/lecture"
        assert "cloudinary.com" in data["secure_url"]

    def test_given_valid_png_when_uploading_then_200(self):
        # Arrange
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        # Act
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

        # Assert
        assert r.status_code == 200
        assert r.json()["mime_type"] == "image/png"
        assert "public_id" in r.json()

    def test_given_admin_when_uploading_then_200(self):
        # Arrange
        jpeg_bytes = b"\xff\xd8\xff" + b"\x00" * 50

        # Act
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

        # Assert
        assert r.status_code == 200
        assert r.json()["valid"] is True
        assert r.json()["public_id"] == "edura/uploads/photo"
