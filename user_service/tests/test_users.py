"""
Integration tests for user_service endpoints.

Requires DATABASE_URL to be set in the environment. Skipped when absent.
"""

import os
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-integration-tests")

from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models import ProfileRole, UserProfile  # noqa: E402

DATABASE_URL = os.getenv("DATABASE_URL")
ALGORITHM = "HS256"
SECRET = os.environ["SECRET_KEY"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(user_id: int, role: str) -> str:
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM)


def _auth(user_id: int, role: str) -> dict:
    return {"Authorization": f"Bearer {_make_token(user_id, role)}"}


# ---------------------------------------------------------------------------
# Fixtures
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
def student(db):
    p = UserProfile(
        user_id=101, role=ProfileRole.student, first_name="Alice", last_name="Student"
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    yield p
    db.delete(p)
    db.commit()


@pytest.fixture()
def teacher(db):
    p = UserProfile(
        user_id=202, role=ProfileRole.teacher, first_name="Bob", last_name="Teacher"
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    yield p
    db.delete(p)
    db.commit()


@pytest.fixture()
def admin(db):
    p = UserProfile(
        user_id=999, role=ProfileRole.admin, first_name="Carol", last_name="Admin"
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    yield p
    db.delete(p)
    db.commit()


# ---------------------------------------------------------------------------
# AC1: GET /{user_id} — own record → 200
# ---------------------------------------------------------------------------


class TestGetUser:
    def test_own_record_returns_200(self, client, student):
        r = client.get("/101", headers=_auth(101, "student"))
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == 101
        assert data["name"] == "Alice Student"
        assert data["role"] == "student"
        assert data["first_name"] == "Alice"
        assert data["last_name"] == "Student"

    def test_admin_can_fetch_any_user(self, client, student, admin):
        r = client.get("/101", headers=_auth(999, "admin"))
        assert r.status_code == 200
        assert r.json()["id"] == 101

    def test_nonexistent_user_returns_404(self, client, admin):
        r = client.get("/99999", headers=_auth(999, "admin"))
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "USER_NOT_FOUND"


# ---------------------------------------------------------------------------
# AC2: GET /{user_id} — other user's record → 403
# ---------------------------------------------------------------------------


class TestGetUserCrossAccess:
    def test_student_cannot_view_other_student(self, client, student, teacher):
        r = client.get("/202", headers=_auth(101, "student"))
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"

    def test_teacher_cannot_view_other_user(self, client, student, teacher):
        r = client.get("/101", headers=_auth(202, "teacher"))
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


# ---------------------------------------------------------------------------
# AC3: PUT /{user_id}/role — admin updates role → 200
# ---------------------------------------------------------------------------


class TestUpdateRole:
    def test_admin_updates_role_to_teacher(self, client, student, admin, db):
        r = client.put(
            "/101/role", json={"role": "teacher"}, headers=_auth(999, "admin")
        )
        assert r.status_code == 200
        assert r.json()["role"] == "teacher"

        # Reset
        student.role = ProfileRole.student
        db.commit()

    def test_invalid_role_returns_422(self, client, student, admin):
        r = client.put(
            "/101/role", json={"role": "superuser"}, headers=_auth(999, "admin")
        )
        assert r.status_code == 422

    def test_student_cannot_update_own_role(self, client, student):
        r = client.put(
            "/101/role", json={"role": "admin"}, headers=_auth(101, "student")
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


# ---------------------------------------------------------------------------
# AC4 / AC5: Decorator blocks wrong role
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_student_blocked_from_admin_list_endpoint(self, client, student):
        r = client.get("/", headers=_auth(101, "student"))
        assert r.status_code == 403

    def test_admin_can_access_list_endpoint(self, client, student, admin):
        r = client.get("/", headers=_auth(999, "admin"))
        assert r.status_code == 200
        assert "items" in r.json()


# ---------------------------------------------------------------------------
# PUT /{user_id} — update own profile
# ---------------------------------------------------------------------------


class TestUpdateProfile:
    def test_student_updates_own_profile(self, client, student, db):
        r = client.put(
            "/101", json={"bio": "I love learning!"}, headers=_auth(101, "student")
        )
        assert r.status_code == 200
        assert r.json()["bio"] == "I love learning!"

        # Reset
        student.bio = None
        db.commit()

    def test_student_cannot_update_other_profile(self, client, student, teacher):
        r = client.put("/202", json={"bio": "Hacked"}, headers=_auth(101, "student"))
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


# ---------------------------------------------------------------------------
# DELETE /{user_id} — admin soft-delete
# ---------------------------------------------------------------------------


class TestDeleteUser:
    def test_admin_can_delete_user(self, client, db, admin):
        p = UserProfile(
            user_id=555, role=ProfileRole.student, first_name="Temp", last_name="User"
        )
        db.add(p)
        db.commit()

        r = client.delete("/555", headers=_auth(999, "admin"))
        assert r.status_code == 204

    def test_student_cannot_delete(self, client, student, teacher):
        r = client.delete("/202", headers=_auth(101, "student"))
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /internal/create-profile — profile created by auth_service at sign-up
# ---------------------------------------------------------------------------


class TestInternalCreateProfile:
    def test_email_signup_stores_the_whole_form_and_applies_the_referral(
        self, client, db, student
    ):
        student.referral_code = "ALICE123"
        db.commit()
        r = client.post(
            "/internal/create-profile",
            json={
                "user_id": 777,
                "role": "student",
                "first_name": "Nimal",
                "last_name": "Perera",
                "mobile_no": "0771234567",
                "school": "Royal College",
                "district": "Colombo",
                "grade": 11,
                "selected_subjects": ["Maths", "Science"],
                "referral_code": "ALICE123",
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert (body["school"], body["district"], body["grade"]) == (
            "Royal College",
            "Colombo",
            11,
        )
        assert body["selected_subjects"] == ["Maths", "Science"]
        assert body["referral_code"]
        created = db.query(UserProfile).filter(UserProfile.user_id == 777).one()
        assert created.referred_by_user_id == student.user_id

        db.query(UserProfile).filter(UserProfile.user_id == 777).delete()
        db.commit()

    def test_invalid_details_are_refused(self, client):
        r = client.post(
            "/internal/create-profile",
            json={
                "user_id": 778,
                "role": "student",
                "first_name": "Bad",
                "last_name": "Phone",
                "mobile_no": "not-a-number",
            },
        )
        assert r.status_code == 422

    def test_name_only_profiles_still_work(self, client, db):
        r = client.post(
            "/internal/create-profile",
            json={"user_id": 779, "role": "admin", "first_name": "A", "last_name": ""},
        )
        assert r.status_code == 201
        db.query(UserProfile).filter(UserProfile.user_id == 779).delete()
        db.commit()

    def test_rolled_back_signup_removes_its_profile(self, client, db):
        client.post(
            "/internal/create-profile",
            json={
                "user_id": 780,
                "role": "student",
                "first_name": "Gone",
                "last_name": "Soon",
            },
        )
        assert db.query(UserProfile).filter(UserProfile.user_id == 780).count() == 1

        assert client.delete("/internal/profiles/780").status_code == 204
        assert db.query(UserProfile).filter(UserProfile.user_id == 780).count() == 0
        # nothing to remove is not an error
        assert client.delete("/internal/profiles/780").status_code == 204
