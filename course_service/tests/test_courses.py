"""
Tests for course_service endpoints and ownership helper.

Unit tests: no DB required.
Integration tests: require DATABASE_URL env var (Neon); skipped when absent.
"""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("SECRET_KEY", "test-secret-for-course-service")
os.environ.setdefault("RABBITMQ_HOST", "localhost")

from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models import Course, CourseStatus, Lesson, Module  # noqa: E402
from router import assert_course_owner  # noqa: E402

DATABASE_URL = os.getenv("DATABASE_URL")
SECRET = os.environ["SECRET_KEY"]
ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_token(user_id: int, role: str) -> str:
    return jwt.encode(
        {"sub": str(user_id), "role": role, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        SECRET,
        algorithm=ALGORITHM,
    )


def _auth(user_id: int, role: str) -> dict:
    return {"Authorization": f"Bearer {_make_token(user_id, role)}"}


# ---------------------------------------------------------------------------
# Unit tests — no database
# ---------------------------------------------------------------------------

class TestAssertCourseOwner:
    def test_matching_teacher_passes(self):
        course = MagicMock()
        course.instructor_id = 42
        assert_course_owner(course, requester_id=42, requester_role="teacher")  # no raise

    def test_mismatched_teacher_raises_403(self):
        from fastapi import HTTPException
        course = MagicMock()
        course.instructor_id = 42
        with pytest.raises(HTTPException) as exc_info:
            assert_course_owner(course, requester_id=99, requester_role="teacher")
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"] == "NOT_COURSE_OWNER"

    def test_admin_bypasses_ownership(self):
        course = MagicMock()
        course.instructor_id = 42
        assert_course_owner(course, requester_id=99, requester_role="admin")  # no raise


class TestStudentBlockedFromCreateCourse:
    def setup_method(self):
        self._client = TestClient(app, raise_server_exceptions=False)

    def test_student_cannot_create_course(self):
        r = self._client.post(
            "/",
            json={"title": "Test", "price": "0.00"},
            headers=_auth(1, "student"),
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"

    def test_no_token_returns_401(self):
        r = self._client.post("/", json={"title": "Test", "price": "0.00"})
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "MISSING_TOKEN"


# ---------------------------------------------------------------------------
# Integration tests — require DATABASE_URL
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_engine():
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set — skipping integration tests")
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def client(db):
    def _override():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def teacher_a_course(db):
    course = Course(
        instructor_id=10,
        title="Teacher A Course",
        price=1000,
        status=CourseStatus.DRAFT,
    )
    db.add(course)
    db.commit()
    db.refresh(course)
    yield course
    try:
        db.delete(course)
        db.commit()
    except Exception:
        db.rollback()


@pytest.fixture()
def published_course(db):
    course = Course(
        instructor_id=10,
        title="Published Course",
        price=500,
        status=CourseStatus.PUBLISHED,
        is_published=True,
    )
    db.add(course)
    db.commit()
    db.refresh(course)
    yield course
    try:
        db.delete(course)
        db.commit()
    except Exception:
        db.rollback()


# ---------------------------------------------------------------------------
# AC1: POST / → 201 + GET /{id} → 200
# ---------------------------------------------------------------------------

class TestCreateAndGetCourse:
    def test_teacher_creates_course_returns_201(self, client):
        r = client.post("/", json={"title": "Python Basics", "price": "500.00"}, headers=_auth(10, "teacher"))
        assert r.status_code == 201
        data = r.json()
        assert data["title"] == "Python Basics"
        assert data["status"] == "DRAFT"
        assert data["teacher_id"] == 10

    def test_get_course_by_id(self, client, teacher_a_course):
        r = client.get(f"/{teacher_a_course.id}", headers=_auth(10, "teacher"))
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == teacher_a_course.id
        assert data["teacher_id"] == 10

    def test_get_nonexistent_course_returns_404(self, client):
        r = client.get("/999999", headers=_auth(10, "teacher"))
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "COURSE_NOT_FOUND"


# ---------------------------------------------------------------------------
# AC4: Ownership enforcement — TEACHER A cannot modify TEACHER B's course
# ---------------------------------------------------------------------------

class TestOwnershipEnforcement:
    def test_teacher_b_cannot_update_teacher_a_course(self, client, teacher_a_course):
        r = client.put(
            f"/{teacher_a_course.id}",
            json={"title": "Hijacked"},
            headers=_auth(99, "teacher"),  # teacher B
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "NOT_COURSE_OWNER"

    def test_admin_can_update_any_course(self, client, teacher_a_course):
        r = client.put(
            f"/{teacher_a_course.id}",
            json={"title": "Admin Updated"},
            headers=_auth(1, "admin"),
        )
        assert r.status_code == 200
        assert r.json()["title"] == "Admin Updated"


# ---------------------------------------------------------------------------
# AC3: Publish triggers RabbitMQ event (pika mocked)
# ---------------------------------------------------------------------------

class TestPublishCourse:
    def test_publish_requires_module_with_lesson(self, client, teacher_a_course):
        r = client.put(
            f"/{teacher_a_course.id}",
            json={"status": "PUBLISHED"},
            headers=_auth(10, "teacher"),
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "PUBLISH_REQUIRES_CONTENT"

    def test_publish_with_content_fires_rabbitmq_event(self, client, db, teacher_a_course):
        module = Module(course_id=teacher_a_course.id, title="Mod 1", position=1)
        db.add(module)
        db.commit()
        db.refresh(module)

        lesson = Lesson(
            module_id=module.id,
            course_id=teacher_a_course.id,
            title="Lesson 1",
            youtube_video_id="dQw4w9WgXcQ",
            position=1,
        )
        db.add(lesson)
        db.commit()

        with patch("router.publish_course_published") as mock_pub:
            r = client.put(
                f"/{teacher_a_course.id}",
                json={"status": "PUBLISHED"},
                headers=_auth(10, "teacher"),
            )
        assert r.status_code == 200
        assert r.json()["status"] == "PUBLISHED"
        mock_pub.assert_called_once_with(teacher_a_course.id, 10, teacher_a_course.title)

        # Cleanup
        db.delete(lesson)
        db.delete(module)
        db.commit()


# ---------------------------------------------------------------------------
# AC2: Module and Lesson creation
# ---------------------------------------------------------------------------

class TestModuleAndLessonCRUD:
    def test_create_module_returns_201(self, client, teacher_a_course):
        r = client.post(
            f"/{teacher_a_course.id}/modules",
            json={"title": "Module 1", "order": 1},
            headers=_auth(10, "teacher"),
        )
        assert r.status_code == 201
        data = r.json()
        assert data["title"] == "Module 1"
        assert data["course_id"] == teacher_a_course.id

    def test_create_lesson_returns_201(self, client, db, teacher_a_course):
        module = Module(course_id=teacher_a_course.id, title="Temp Module", position=0)
        db.add(module)
        db.commit()
        db.refresh(module)

        r = client.post(
            f"/{teacher_a_course.id}/modules/{module.id}/lessons",
            json={"title": "Lesson 1", "youtube_video_id": "dQw4w9WgXcQ", "order": 1},
            headers=_auth(10, "teacher"),
        )
        assert r.status_code == 201
        data = r.json()
        assert data["youtube_video_id"] == "dQw4w9WgXcQ"
        assert data["module_id"] == module.id

        db.delete(module)
        db.commit()


# ---------------------------------------------------------------------------
# AC5: Students see only PUBLISHED courses
# ---------------------------------------------------------------------------

class TestStudentCourseBrowse:
    def test_student_only_sees_published_courses(self, client, teacher_a_course, published_course):
        r = client.get("/", headers=_auth(5, "student"))
        assert r.status_code == 200
        ids = [c["id"] for c in r.json()["items"]]
        assert published_course.id in ids
        assert teacher_a_course.id not in ids  # DRAFT — not visible

    def test_student_cannot_get_draft_course_directly(self, client, teacher_a_course):
        r = client.get(f"/{teacher_a_course.id}", headers=_auth(5, "student"))
        assert r.status_code == 404
