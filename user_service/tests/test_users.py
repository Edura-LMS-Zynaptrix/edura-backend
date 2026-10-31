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
    def test_given_student_when_reading_own_record_then_200(self, client, student):
        # Act
        r = client.get("/101", headers=_auth(101, "student"))

        # Assert
        assert r.status_code == 200

        # Act
        data = r.json()

        # Assert
        assert data["id"] == 101
        assert data["name"] == "Alice Student"
        assert data["role"] == "student"
        assert data["first_name"] == "Alice"
        assert data["last_name"] == "Student"

    def test_given_admin_when_reading_any_user_then_200(self, client, student, admin):
        # Act
        r = client.get("/101", headers=_auth(999, "admin"))

        # Assert
        assert r.status_code == 200
        assert r.json()["id"] == 101

    def test_given_unknown_user_when_read_then_404_user_not_found(self, client, admin):
        # Act
        r = client.get("/99999", headers=_auth(999, "admin"))

        # Assert
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "USER_NOT_FOUND"


# ---------------------------------------------------------------------------
# AC2: GET /{user_id} — other user's record → 403
# ---------------------------------------------------------------------------


class TestGetUserCrossAccess:
    def test_given_student_when_reading_another_student_then_403_insufficient_permissions(
        self, client, student, teacher
    ):
        # Act
        r = client.get("/202", headers=_auth(101, "student"))

        # Assert
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"

    def test_given_teacher_when_reading_another_user_then_403_insufficient_permissions(
        self, client, student, teacher
    ):
        # Act
        r = client.get("/101", headers=_auth(202, "teacher"))

        # Assert
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


# ---------------------------------------------------------------------------
# AC3: PUT /{user_id}/role — admin updates role → 200
# ---------------------------------------------------------------------------


class TestUpdateRole:
    def test_given_admin_when_updating_role_to_teacher_then_role_changes(
        self, client, student, admin, db
    ):
        # Act
        r = client.put(
            "/101/role", json={"role": "teacher"}, headers=_auth(999, "admin")
        )

        # Assert
        assert r.status_code == 200
        assert r.json()["role"] == "teacher"

        # Reset
        student.role = ProfileRole.student
        db.commit()

    def test_given_invalid_role_when_updating_role_then_422(
        self, client, student, admin
    ):
        # Act
        r = client.put(
            "/101/role", json={"role": "superuser"}, headers=_auth(999, "admin")
        )

        # Assert
        assert r.status_code == 422

    def test_given_student_when_updating_own_role_then_403_insufficient_permissions(
        self, client, student
    ):
        # Act
        r = client.put(
            "/101/role", json={"role": "admin"}, headers=_auth(101, "student")
        )

        # Assert
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


# ---------------------------------------------------------------------------
# AC4 / AC5: Decorator blocks wrong role
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_given_student_when_admin_list_endpoint_called_then_403(
        self, client, student
    ):
        # Act
        r = client.get("/", headers=_auth(101, "student"))

        # Assert
        assert r.status_code == 403

    def test_given_admin_when_list_endpoint_called_then_200(
        self, client, student, admin
    ):
        # Act
        r = client.get("/", headers=_auth(999, "admin"))

        # Assert
        assert r.status_code == 200
        assert "items" in r.json()


# ---------------------------------------------------------------------------
# PUT /{user_id} — update own profile
# ---------------------------------------------------------------------------


class TestUpdateProfile:
    def test_given_student_when_updating_own_profile_then_200(
        self, client, student, db
    ):
        # Act
        r = client.put(
            "/101", json={"bio": "I love learning!"}, headers=_auth(101, "student")
        )

        # Assert
        assert r.status_code == 200
        assert r.json()["bio"] == "I love learning!"

        # Reset
        student.bio = None
        db.commit()

    def test_given_student_when_updating_another_profile_then_403_insufficient_permissions(
        self, client, student, teacher
    ):
        # Act
        r = client.put("/202", json={"bio": "Hacked"}, headers=_auth(101, "student"))

        # Assert
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


# ---------------------------------------------------------------------------
# DELETE /{user_id} — admin soft-delete
# ---------------------------------------------------------------------------


class TestDeleteUser:
    def test_given_admin_when_deleting_user_then_204(self, client, db, admin):
        # Arrange
        p = UserProfile(
            user_id=555, role=ProfileRole.student, first_name="Temp", last_name="User"
        )
        db.add(p)
        db.commit()

        # Act
        r = client.delete("/555", headers=_auth(999, "admin"))

        # Assert
        assert r.status_code == 204

    def test_given_student_when_deleting_user_then_403(self, client, student, teacher):
        # Act
        r = client.delete("/202", headers=_auth(101, "student"))

        # Assert
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Events: user_service builds the profile when auth_service announces a sign-up
# ---------------------------------------------------------------------------


@pytest.fixture()
def published(monkeypatch):
    """Capture what this service announces (there is no broker in tests)."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "profile_service.publish",
        lambda key, payload: events.append((key, payload)) or True,
    )
    monkeypatch.setattr(
        "router.publish", lambda key, payload: events.append((key, payload)) or True
    )
    return events


def _registered(user_id: int, **profile) -> dict:
    return {
        "user_id": user_id,
        "email": f"user{user_id}@example.com",
        "role": "student",
        "is_active": True,
        "is_email_verified": False,
        "created_at": "2026-10-20T08:00:00+00:00",
        "profile": profile or None,
    }


class TestSignupEvents:
    def test_given_registration_event_when_consumed_then_profile_is_built_and_account_stored(
        self, db, published
    ):
        # Arrange
        from event_consumer import handle
        from models import ReplicaAccount

        handle(
            "auth.user-registered",
            _registered(
                777,
                first_name="Nimal",
                last_name="Perera",
                mobile_no="0771234567",
                school="Royal College",
                district="Colombo",
                grade=11,
                selected_subjects=["Maths", "Science"],
            ),
        )

        # Act
        profile = db.query(UserProfile).filter(UserProfile.user_id == 777).one()

        # Assert
        assert (profile.school, profile.district, profile.grade) == (
            "Royal College",
            "Colombo",
            11,
        )
        assert profile.selected_subjects == ["Maths", "Science"]
        assert profile.referral_code

        # Act
        account = db.get(ReplicaAccount, 777)

        # Assert
        assert account.email == "user777@example.com" and account.is_active is True

        # the profile is announced for every service that keeps a copy
        key, payload = published[-1]

        # Act and assert
        assert key == "user.profile-upserted"
        assert payload["user_id"] == 777 and payload["full_name"] == "Nimal Perera"

        db.query(UserProfile).filter(UserProfile.user_id == 777).delete()
        db.query(ReplicaAccount).filter(ReplicaAccount.user_id == 777).delete()
        db.commit()

    def test_given_registration_event_when_repeated_then_no_second_profile_is_created(
        self, db, published
    ):
        # Arrange
        from event_consumer import handle

        event = _registered(778, first_name="Once", last_name="Only")
        handle("auth.user-registered", event)
        handle("auth.user-registered", event)

        # Act and assert
        assert db.query(UserProfile).filter(UserProfile.user_id == 778).count() == 1
        assert len([k for k, _ in published if k == "user.profile-upserted"]) == 1

        db.query(UserProfile).filter(UserProfile.user_id == 778).delete()
        db.commit()

    def test_given_signup_with_referral_code_when_consumed_then_referral_is_applied(
        self, db, student, published
    ):
        # Arrange
        from event_consumer import handle

        student.referral_code = "ALICE123"
        db.commit()
        handle(
            "auth.user-registered",
            _registered(
                779, first_name="Kamal", last_name="Silva", referral_code="ALICE123"
            ),
        )

        # Act
        created = db.query(UserProfile).filter(UserProfile.user_id == 779).one()

        # Assert
        assert created.referred_by_user_id == student.user_id

        db.query(UserProfile).filter(UserProfile.user_id == 779).delete()
        db.commit()

    def test_given_admin_created_without_form_when_event_consumed_then_a_profile_is_still_created(
        self, db, published
    ):
        # Arrange
        from event_consumer import handle

        event = _registered(780, first_name="New", last_name="Admin")
        event["role"] = "admin"
        handle("auth.user-registered", event)

        # Act
        profile = db.query(UserProfile).filter(UserProfile.user_id == 780).one()

        # Assert
        assert profile.role == ProfileRole.admin

        db.query(UserProfile).filter(UserProfile.user_id == 780).delete()
        db.commit()

    def test_given_account_change_event_when_consumed_then_user_listing_is_updated(
        self, client, db, student, admin, published
    ):
        # Arrange
        from event_consumer import handle

        handle("auth.user-registered", _registered(student.user_id))
        handle(
            "auth.account-updated",
            {
                "user_id": student.user_id,
                "email": "alice@example.com",
                "is_active": False,
            },
        )
        r = client.get("/", headers=_auth(999, "admin"))

        # Act
        row = next(i for i in r.json()["items"] if i["id"] == student.user_id)

        # Assert
        assert row["email"] == "alice@example.com" and row["is_active"] is False

    def test_given_enrollment_events_when_consumed_then_referrer_is_credited_once_and_manual_grants_earn_nothing(
        self, db, student, published, monkeypatch
    ):
        # Arrange
        import event_consumer

        credited = []
        monkeypatch.setattr(
            event_consumer,
            "credit_on_enrollment",
            lambda db, sid: credited.append(sid) or True,
        )

        # Act
        event_consumer.handle(
            "enrollment.activated", {"student_id": 5, "source": "manual"}
        )

        # Assert
        assert credited == []

        # Act
        event_consumer.handle(
            "enrollment.activated", {"student_id": 5, "source": "payment"}
        )

        # Assert
        assert credited == [5]


class TestProfileAnnouncements:
    def test_given_profile_when_updated_and_deleted_then_each_change_is_announced(
        self, client, student, admin, published
    ):
        # Act
        r = client.put("/101", json={"bio": "Hello"}, headers=_auth(101, "student"))

        # Assert
        assert r.status_code == 200
        assert published[-1][0] == "user.profile-upserted"

        # Act
        r = client.delete("/101", headers=_auth(999, "admin"))

        # Assert
        assert r.status_code == 204
        assert published[-1] == ("user.profile-deleted", {"user_id": 101})

    def test_given_stream_when_created_and_deleted_then_each_change_is_announced(
        self, client, admin, published
    ):
        # Act
        r = client.post(
            "/streams",
            json={"name": "Announce Test", "subjects": ["Maths"]},
            headers=_auth(999, "admin"),
        )

        # Assert
        assert r.status_code == 201
        key, payload = published[-1]

        # Act and assert
        assert key == "user.stream-upserted" and payload["name"] == "Announce Test"

        # Act
        stream_id = r.json()["id"]
        client.delete(f"/streams/{stream_id}", headers=_auth(999, "admin"))

        # Assert
        assert published[-1] == ("user.stream-deleted", {"id": stream_id})


class TestNoDirectCalls:
    def test_given_removed_internal_endpoints_when_called_then_they_are_not_available(
        self, client
    ):
        # Arrange
        for path in (
            "/internal/students",
            "/internal/names?ids=1",
            "/internal/profiles",
            "/internal/stats",
            "/internal/student-breakdown",
        ):
            assert client.get(path).status_code in (404, 401, 403, 422), path

        # Act and assert
        assert client.post("/internal/create-profile", json={}).status_code in (
            404,
            405,
        )
