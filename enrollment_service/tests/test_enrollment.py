import os
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Setup test DB (SQLite in-memory with StaticPool for thread isolation)
TEST_DATABASE_URL = "sqlite://"

os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import database
import services
from database import Base, get_db
from models import (
    ClassEnrollment,
    ClassPackage,
    Enrollment,
    EnrollmentStatus,
    ReplicaCourse,
    ReplicaStream,
    ReplicaUser,
)

from shared.auth import ALGORITHM, SECRET_KEY

# Create SQLite engine
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Patch SessionLocal in database module and consumer
database.engine = engine
database.SessionLocal = TestingSessionLocal


Base.metadata.create_all(bind=engine)

import access_events
import consumer
import events
import router as router_module
from main import app


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)


def _auth(user_id: int, role: str = "student") -> dict:
    token = jwt.encode(
        {"sub": str(user_id), "role": role.upper()}, SECRET_KEY, algorithm=ALGORITHM
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    session = TestingSessionLocal()
    yield session
    session.close()


@pytest.fixture()
def published(monkeypatch):
    """Everything this service announces (there is no broker in tests)."""
    sent: list[tuple[str, dict]] = []

    def capture(key, payload):
        sent.append((key, payload))
        return True

    monkeypatch.setattr(access_events, "publish", capture)
    monkeypatch.setattr(events, "publish", capture)
    return sent


@pytest.fixture()
def deliver(monkeypatch):
    """Hand an event to the consumer, as the broker would."""
    monkeypatch.setattr(consumer, "SessionLocal", TestingSessionLocal)
    return consumer.handle


def _access_events(published, student_id):
    return [
        p
        for k, p in published
        if k == "access.changed" and p["student_id"] == student_id
    ]


def _course(course_id=20, grade=11, status="PUBLISHED", streams=None, title="Maths"):
    return {
        "id": course_id,
        "title": title,
        "grade": grade,
        "status": status,
        "stream_ids": streams or [],
        "course_type": "video",
        "price": 1000,
    }


# ---------------------------------------------------------------------------
# GET /api/enrollments: a signed-in student sees their own, an admin any
# ---------------------------------------------------------------------------
def test_given_existing_enrollment_when_looked_up_then_active_enrollment_returned(db):
    # Arrange
    enrollment = services.activate_or_extend_enrollment(db, student_id=10, course_id=20)

    # Act
    r = client.get("/api/enrollments?student_id=10&course_id=20", headers=_auth(10))

    # Assert
    assert r.status_code == 200

    # Act
    data = r.json()

    # Assert
    assert data["enrollment_id"] == enrollment.id
    assert data["student_id"] == 10 and data["course_id"] == 20
    assert data["status"] == "ACTIVE" and data["expires_at"] is not None


def test_given_no_enrollment_when_looked_up_then_404():
    # Act
    r = client.get("/api/enrollments?student_id=99&course_id=99", headers=_auth(99))

    # Assert
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_given_missing_token_or_other_student_when_looking_up_enrollment_then_login_required_and_access_is_private(
    db,
):
    # Arrange
    services.activate_or_extend_enrollment(db, student_id=10, course_id=20)
    url = "/api/enrollments?student_id=10&course_id=20"

    # Act and assert
    assert client.get(url).status_code == 401
    assert client.get(url, headers=_auth(11)).status_code == 403  # someone else's
    assert client.get(url, headers=_auth(1, "admin")).status_code == 200
    # the old unauthenticated alias is gone
    assert client.get("/enrollments?student_id=10&course_id=20").status_code == 404


# ---------------------------------------------------------------------------
# payment.success: enroll, extend on a repeat payment, tell everyone
# ---------------------------------------------------------------------------
def test_given_payment_success_event_when_consumed_then_enrollment_is_activated_and_announced(
    db, deliver, published
):
    # Arrange
    deliver("payment.success", {"student_id": 1, "course_id": 1, "order_id": "ORD-123"})

    # Act
    enrollment = (
        db.query(Enrollment)
        .filter(Enrollment.student_id == 1, Enrollment.course_id == 1)
        .one()
    )

    # Assert
    assert (
        enrollment.status == EnrollmentStatus.ACTIVE
        and enrollment.expires_at is not None
    )

    keys = [k for k, _ in published]

    # Act and assert
    assert "enrollment.activated" in keys  # referral credit and progress listen to this
    assert (
        "enrollment.changed",
        {"student_id": 1, "course_id": 1, "status": "ACTIVE", "source": "payment"},
    ) in published

    # Act
    (access,) = _access_events(published, 1)

    # Assert
    assert access["courses"] == [{"id": 1, "until": None}]


def test_given_active_enrollment_when_payment_repeats_then_expiry_is_extended_without_a_second_enrollment(
    db, deliver, published
):
    # Arrange
    deliver("payment.success", {"student_id": 5, "course_id": 5, "order_id": "ORD-001"})
    first = db.query(Enrollment).filter(Enrollment.student_id == 5).one().expires_at

    deliver("payment.success", {"student_id": 5, "course_id": 5, "order_id": "ORD-002"})

    # Act
    rows = (
        db.query(Enrollment)
        .filter(Enrollment.student_id == 5, Enrollment.course_id == 5)
        .all()
    )

    # Assert
    assert len(rows) == 1
    db.refresh(rows[0])

    # Act and assert
    assert rows[0].expires_at > first


def test_given_expired_subscription_when_expiry_event_consumed_then_enrollment_suspended_and_access_removed(
    db, deliver, published
):
    # Arrange
    services.activate_or_extend_enrollment(db, student_id=7, course_id=7)

    deliver("subscription.expired", {"student_id": 7, "course_id": 7})

    enrollment = db.query(Enrollment).filter(Enrollment.student_id == 7).one()

    # Act and assert
    assert enrollment.status == EnrollmentStatus.SUSPENDED
    assert _access_events(published, 7)[-1]["courses"] == []


# ---------------------------------------------------------------------------
# What a student can open: enrollment, one lesson, or a class package
# ---------------------------------------------------------------------------
def _package(db, grade=11, stream_id=None, active=True) -> ClassPackage:
    pkg = ClassPackage(
        name=f"Grade {grade}",
        grade=grade,
        stream_id=stream_id,
        price=0,
        is_active=active,
    )
    db.add(pkg)
    db.commit()
    return pkg


def _hold(db, user_id, pkg, until):
    db.add(
        ClassEnrollment(
            user_id=user_id, class_package_id=pkg.id, status="active", expires_at=until
        )
    )
    db.commit()


def test_given_class_package_when_it_is_active_then_published_courses_of_its_grade_are_open_until_it_expires(
    db, deliver, published
):
    # Arrange
    deliver("course.upserted", _course(20, grade=11))
    deliver("course.upserted", _course(21, grade=9))
    deliver("course.upserted", _course(22, grade=11, status="DRAFT"))
    until = datetime.now(timezone.utc) + timedelta(days=30)
    _hold(db, 3, _package(db, grade=11), until)

    # Act
    access = access_events.compute_access(db, 3)

    # Assert
    assert access["courses"] == [{"id": 20, "until": until.isoformat()}]


def test_given_expired_or_inactive_package_when_access_checked_then_nothing_is_open(
    db, deliver
):
    # Arrange
    deliver("course.upserted", _course(20, grade=11))
    _hold(db, 3, _package(db, grade=11), datetime.now(timezone.utc) - timedelta(days=1))

    _hold(
        db,
        4,
        _package(db, grade=11, active=False),
        datetime.now(timezone.utc) + timedelta(days=5),
    )

    # Act and assert
    assert access_events.compute_access(db, 3)["courses"] == []
    assert access_events.compute_access(db, 4)["courses"] == []


def test_given_lesson_grant_when_access_checked_then_only_that_lesson_is_open_unless_whole_course_is_open(
    db,
):
    # Arrange
    from models import LessonAccess

    db.add_all(
        [
            LessonAccess(student_id=9, course_id=20, module_id=200),
            LessonAccess(student_id=9, course_id=30, module_id=300),
        ]
    )
    db.add(Enrollment(student_id=9, course_id=30, status=EnrollmentStatus.ACTIVE))
    db.commit()

    # Act
    access = access_events.compute_access(db, 9)

    # Assert
    assert access["courses"] == [{"id": 30, "until": None}]
    assert access["lessons"] == [200]  # 300 is already covered by the whole course 30


def test_given_package_holder_when_a_course_is_published_then_access_is_refreshed(
    db, deliver, published
):
    # Arrange
    deliver("course.upserted", _course(20, grade=11, status="DRAFT"))
    _hold(
        db, 3, _package(db, grade=11), datetime.now(timezone.utc) + timedelta(days=30)
    )
    published.clear()

    deliver("course.upserted", _course(20, grade=11, status="PUBLISHED"))

    # Act and assert
    assert _access_events(published, 3)[-1]["courses"][0]["id"] == 20


def test_given_course_renamed_when_catalogue_event_consumed_then_nobodys_access_is_announced_again(
    db, deliver, published
):
    # Arrange
    deliver("course.upserted", _course(20, grade=11))
    _hold(
        db, 3, _package(db, grade=11), datetime.now(timezone.utc) + timedelta(days=30)
    )
    published.clear()

    # Act
    deliver("course.upserted", _course(20, grade=11, title="Maths (new name)"))

    # Assert
    assert published == []


def test_given_course_copy_when_catalogue_events_arrive_then_copy_follows_the_catalogue(
    db, deliver
):
    # Arrange
    deliver("course.upserted", _course(20, grade=11))

    # Act and assert
    assert db.get(ReplicaCourse, 20).title == "Maths"
    deliver("course.deleted", {"id": 20})

    # Act and assert
    assert db.get(ReplicaCourse, 20) is None


# ---------------------------------------------------------------------------
# Admin pickers use the local copies of students and streams
# ---------------------------------------------------------------------------
def _student(deliver, user_id, name, grade=11, school="Royal"):
    deliver(
        "user.profile-upserted",
        {
            "user_id": user_id,
            "role": "student",
            "full_name": name,
            "grade": grade,
            "district": "Colombo",
            "school": school,
        },
    )


def test_given_course_when_eligible_students_requested_then_only_students_in_grade_band_without_access_are_listed(
    deliver, db
):
    # Arrange
    deliver("course.upserted", _course(20, grade=10))
    _student(deliver, 1, "Nimal Perera", grade=10)
    _student(deliver, 2, "Kamal Silva", grade=11)
    _student(deliver, 3, "Sunil Fernando", grade=9)
    db.add(Enrollment(student_id=2, course_id=20, status=EnrollmentStatus.ACTIVE))
    db.commit()

    # Act
    r = client.get(
        "/api/enrollments/admin/eligible?course_id=20", headers=_auth(1, "admin")
    )

    # Assert
    assert r.status_code == 200
    assert [s["full_name"] for s in r.json()] == [
        "Nimal Perera"
    ]  # 11 is in the band but already enrolled; 9 is not

    # Act
    found = client.get(
        "/api/enrollments/admin/eligible?course_id=20&search=nimal",
        headers=_auth(1, "admin"),
    )

    # Assert
    assert len(found.json()) == 1

    # Act and assert
    assert (
        client.get(
            "/api/enrollments/admin/eligible?course_id=999", headers=_auth(1, "admin")
        ).status_code
        == 404
    )


def test_given_admin_when_granting_access_then_student_is_enrolled_announced_and_listed_by_name(
    deliver, db, published
):
    # Arrange
    deliver("course.upserted", _course(20, grade=11))
    _student(deliver, 4, "Amali Jayasinghe")

    # Act
    r = client.post(
        "/api/enrollments/admin/grant",
        json={"course_id": 20, "user_ids": [4]},
        headers=_auth(1, "admin"),
    )

    # Assert
    assert r.status_code == 200 and r.json()["granted"] == 1
    assert _access_events(published, 4)[-1]["courses"] == [{"id": 20, "until": None}]
    assert (
        "enrollment.changed",
        {"student_id": 4, "course_id": 20, "status": "ACTIVE", "source": "manual"},
    ) in published

    # Act
    listing = client.get(
        "/api/enrollments/admin/enrolled?course_id=20", headers=_auth(1, "admin")
    ).json()

    # Assert
    assert [(s["user_id"], s["full_name"], s["is_manual"]) for s in listing] == [
        ("4", "Amali Jayasinghe", True)
    ]

    published.clear()

    # Act
    r = client.delete(
        "/api/enrollments/admin/revoke?course_id=20&user_id=4",
        headers=_auth(1, "admin"),
    )

    # Assert
    assert r.status_code == 200
    assert _access_events(published, 4)[-1]["courses"] == []


def test_given_stream_copy_when_packages_listed_then_package_names_use_the_local_copy(
    deliver, db
):
    # Arrange
    deliver(
        "user.stream-upserted",
        {"id": 3, "name": "Biology", "is_active": True, "subjects": []},
    )
    _package(db, grade=12, stream_id=3)

    # Act
    r = client.get("/api/enrollments/class-packages")

    # Assert
    assert r.json()[0]["stream_name"] == "Biology"
    deliver("user.stream-deleted", {"id": 3})

    # Act and assert
    assert db.get(ReplicaStream, 3) is None


def test_given_active_streams_when_packages_auto_generated_then_one_package_per_grade_and_stream_is_created(
    deliver, db
):
    # Arrange
    deliver(
        "user.stream-upserted",
        {"id": 3, "name": "Biology", "is_active": True, "subjects": []},
    )

    # Act
    r = client.post(
        "/api/enrollments/admin/class-packages/auto-generate", headers=_auth(1, "admin")
    )

    # Assert
    assert r.status_code == 200

    # Act
    names = r.json()["created"]

    # Assert
    assert (
        "Grade 5" in names
        and "Grade 12 Biology" in names
        and "Grade 13 Biology" in names
    )


def test_given_package_holders_when_package_changes_then_change_is_announced_to_holders(
    deliver, db, published
):
    # Arrange
    deliver("course.upserted", _course(20, grade=11))
    pkg = _package(db, grade=11)
    _hold(db, 3, pkg, datetime.now(timezone.utc) + timedelta(days=30))
    published.clear()

    # Act
    r = client.patch(
        f"/api/enrollments/admin/class-packages/{pkg.id}",
        json={"is_active": False},
        headers=_auth(1, "admin"),
    )

    # Assert
    assert r.status_code == 200
    assert _access_events(published, 3)[-1]["courses"] == []


# ---------------------------------------------------------------------------
# No direct calls
# ---------------------------------------------------------------------------
def test_given_removed_internal_endpoints_when_requested_then_404():
    for path in (
        "/internal/access?student_id=1&course_id=1",
        "/internal/course-counts",
        "/internal/students/1/accessible",
    ):
        assert client.get(path).status_code == 404, path


def test_given_user_events_when_consumed_then_service_reads_people_from_its_own_copy(
    deliver, db
):
    # Arrange
    _student(deliver, 8, "Local Copy")

    # Act and assert
    assert router_module.display_names(db, [8, 9]) == {8: "Local Copy"}

    # Act and assert
    assert db.get(ReplicaUser, 8).role == "student"
