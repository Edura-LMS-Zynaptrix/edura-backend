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
def test_get_enrollment_success(db):
    enrollment = services.activate_or_extend_enrollment(db, student_id=10, course_id=20)

    r = client.get("/api/enrollments?student_id=10&course_id=20", headers=_auth(10))

    assert r.status_code == 200
    data = r.json()
    assert data["enrollment_id"] == enrollment.id
    assert data["student_id"] == 10 and data["course_id"] == 20
    assert data["status"] == "ACTIVE" and data["expires_at"] is not None


def test_get_enrollment_not_found_returns_404():
    r = client.get("/api/enrollments?student_id=99&course_id=99", headers=_auth(99))
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_enrollment_lookup_needs_login_and_is_private(db):
    services.activate_or_extend_enrollment(db, student_id=10, course_id=20)
    url = "/api/enrollments?student_id=10&course_id=20"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=_auth(11)).status_code == 403  # someone else's
    assert client.get(url, headers=_auth(1, "admin")).status_code == 200
    # the old unauthenticated alias is gone
    assert client.get("/enrollments?student_id=10&course_id=20").status_code == 404


# ---------------------------------------------------------------------------
# payment.success: enroll, extend on a repeat payment, tell everyone
# ---------------------------------------------------------------------------
def test_payment_success_activates_the_enrollment_and_announces_it(
    db, deliver, published
):
    deliver("payment.success", {"student_id": 1, "course_id": 1, "order_id": "ORD-123"})

    enrollment = (
        db.query(Enrollment)
        .filter(Enrollment.student_id == 1, Enrollment.course_id == 1)
        .one()
    )
    assert (
        enrollment.status == EnrollmentStatus.ACTIVE
        and enrollment.expires_at is not None
    )

    keys = [k for k, _ in published]
    assert "enrollment.activated" in keys  # referral credit and progress listen to this
    assert (
        "enrollment.changed",
        {"student_id": 1, "course_id": 1, "status": "ACTIVE", "source": "payment"},
    ) in published
    (access,) = _access_events(published, 1)
    assert access["courses"] == [{"id": 1, "until": None}]


def test_a_repeated_payment_extends_expiry_without_a_second_enrollment(
    db, deliver, published
):
    deliver("payment.success", {"student_id": 5, "course_id": 5, "order_id": "ORD-001"})
    first = db.query(Enrollment).filter(Enrollment.student_id == 5).one().expires_at

    deliver("payment.success", {"student_id": 5, "course_id": 5, "order_id": "ORD-002"})

    rows = (
        db.query(Enrollment)
        .filter(Enrollment.student_id == 5, Enrollment.course_id == 5)
        .all()
    )
    assert len(rows) == 1
    db.refresh(rows[0])
    assert rows[0].expires_at > first


def test_subscription_expiry_suspends_and_removes_access(db, deliver, published):
    services.activate_or_extend_enrollment(db, student_id=7, course_id=7)

    deliver("subscription.expired", {"student_id": 7, "course_id": 7})

    enrollment = db.query(Enrollment).filter(Enrollment.student_id == 7).one()
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


def test_class_package_opens_published_courses_of_its_grade_until_it_expires(
    db, deliver, published
):
    deliver("course.upserted", _course(20, grade=11))
    deliver("course.upserted", _course(21, grade=9))
    deliver("course.upserted", _course(22, grade=11, status="DRAFT"))
    until = datetime.now(timezone.utc) + timedelta(days=30)
    _hold(db, 3, _package(db, grade=11), until)

    access = access_events.compute_access(db, 3)

    assert access["courses"] == [{"id": 20, "until": until.isoformat()}]


def test_an_expired_or_inactive_package_opens_nothing(db, deliver):
    deliver("course.upserted", _course(20, grade=11))
    _hold(db, 3, _package(db, grade=11), datetime.now(timezone.utc) - timedelta(days=1))
    _hold(
        db,
        4,
        _package(db, grade=11, active=False),
        datetime.now(timezone.utc) + timedelta(days=5),
    )
    assert access_events.compute_access(db, 3)["courses"] == []
    assert access_events.compute_access(db, 4)["courses"] == []


def test_a_lesson_grant_opens_just_that_lesson_unless_the_whole_course_is_open(db):
    from models import LessonAccess

    db.add_all(
        [
            LessonAccess(student_id=9, course_id=20, module_id=200),
            LessonAccess(student_id=9, course_id=30, module_id=300),
        ]
    )
    db.add(Enrollment(student_id=9, course_id=30, status=EnrollmentStatus.ACTIVE))
    db.commit()
    access = access_events.compute_access(db, 9)
    assert access["courses"] == [{"id": 30, "until": None}]
    assert access["lessons"] == [200]  # 300 is already covered by the whole course 30


def test_a_package_holders_access_is_refreshed_when_a_course_is_published(
    db, deliver, published
):
    deliver("course.upserted", _course(20, grade=11, status="DRAFT"))
    _hold(
        db, 3, _package(db, grade=11), datetime.now(timezone.utc) + timedelta(days=30)
    )
    published.clear()

    deliver("course.upserted", _course(20, grade=11, status="PUBLISHED"))

    assert _access_events(published, 3)[-1]["courses"][0]["id"] == 20


def test_renaming_a_course_does_not_re_announce_anyones_access(db, deliver, published):
    deliver("course.upserted", _course(20, grade=11))
    _hold(
        db, 3, _package(db, grade=11), datetime.now(timezone.utc) + timedelta(days=30)
    )
    published.clear()

    deliver("course.upserted", _course(20, grade=11, title="Maths (new name)"))

    assert published == []


def test_the_course_copy_follows_the_catalogue(db, deliver):
    deliver("course.upserted", _course(20, grade=11))
    assert db.get(ReplicaCourse, 20).title == "Maths"
    deliver("course.deleted", {"id": 20})
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


def test_eligible_students_are_those_in_the_course_grade_band_without_access(
    deliver, db
):
    deliver("course.upserted", _course(20, grade=10))
    _student(deliver, 1, "Nimal Perera", grade=10)
    _student(deliver, 2, "Kamal Silva", grade=11)
    _student(deliver, 3, "Sunil Fernando", grade=9)
    db.add(Enrollment(student_id=2, course_id=20, status=EnrollmentStatus.ACTIVE))
    db.commit()

    r = client.get(
        "/api/enrollments/admin/eligible?course_id=20", headers=_auth(1, "admin")
    )

    assert r.status_code == 200
    assert [s["full_name"] for s in r.json()] == [
        "Nimal Perera"
    ]  # 11 is in the band but already enrolled; 9 is not
    found = client.get(
        "/api/enrollments/admin/eligible?course_id=20&search=nimal",
        headers=_auth(1, "admin"),
    )
    assert len(found.json()) == 1
    assert (
        client.get(
            "/api/enrollments/admin/eligible?course_id=999", headers=_auth(1, "admin")
        ).status_code
        == 404
    )


def test_admin_grant_enrolls_announces_and_lists_by_name(deliver, db, published):
    deliver("course.upserted", _course(20, grade=11))
    _student(deliver, 4, "Amali Jayasinghe")

    r = client.post(
        "/api/enrollments/admin/grant",
        json={"course_id": 20, "user_ids": [4]},
        headers=_auth(1, "admin"),
    )

    assert r.status_code == 200 and r.json()["granted"] == 1
    assert _access_events(published, 4)[-1]["courses"] == [{"id": 20, "until": None}]
    assert (
        "enrollment.changed",
        {"student_id": 4, "course_id": 20, "status": "ACTIVE", "source": "manual"},
    ) in published
    listing = client.get(
        "/api/enrollments/admin/enrolled?course_id=20", headers=_auth(1, "admin")
    ).json()
    assert [(s["user_id"], s["full_name"], s["is_manual"]) for s in listing] == [
        ("4", "Amali Jayasinghe", True)
    ]

    published.clear()
    r = client.delete(
        "/api/enrollments/admin/revoke?course_id=20&user_id=4",
        headers=_auth(1, "admin"),
    )
    assert r.status_code == 200
    assert _access_events(published, 4)[-1]["courses"] == []


def test_package_names_use_the_local_stream_copy(deliver, db):
    deliver(
        "user.stream-upserted",
        {"id": 3, "name": "Biology", "is_active": True, "subjects": []},
    )
    _package(db, grade=12, stream_id=3)
    r = client.get("/api/enrollments/class-packages")
    assert r.json()[0]["stream_name"] == "Biology"
    deliver("user.stream-deleted", {"id": 3})
    assert db.get(ReplicaStream, 3) is None


def test_auto_generate_makes_a_package_per_grade_and_active_stream(deliver, db):
    deliver(
        "user.stream-upserted",
        {"id": 3, "name": "Biology", "is_active": True, "subjects": []},
    )
    r = client.post(
        "/api/enrollments/admin/class-packages/auto-generate", headers=_auth(1, "admin")
    )
    assert r.status_code == 200
    names = r.json()["created"]
    assert (
        "Grade 5" in names
        and "Grade 12 Biology" in names
        and "Grade 13 Biology" in names
    )


def test_class_package_changes_are_announced_to_holders(deliver, db, published):
    deliver("course.upserted", _course(20, grade=11))
    pkg = _package(db, grade=11)
    _hold(db, 3, pkg, datetime.now(timezone.utc) + timedelta(days=30))
    published.clear()

    r = client.patch(
        f"/api/enrollments/admin/class-packages/{pkg.id}",
        json={"is_active": False},
        headers=_auth(1, "admin"),
    )

    assert r.status_code == 200
    assert _access_events(published, 3)[-1]["courses"] == []


# ---------------------------------------------------------------------------
# No direct calls
# ---------------------------------------------------------------------------
def test_the_old_internal_endpoints_are_gone():
    for path in (
        "/internal/access?student_id=1&course_id=1",
        "/internal/course-counts",
        "/internal/students/1/accessible",
    ):
        assert client.get(path).status_code == 404, path


def test_this_service_reads_people_from_its_own_copy(deliver, db):
    _student(deliver, 8, "Local Copy")
    assert router_module.display_names(db, [8, 9]) == {8: "Local Copy"}
    assert db.get(ReplicaUser, 8).role == "student"
