"""Enrollment service gaps on SQLite: own enrollments and access, class packages
(free enroll, admin CRUD, holders), manual course/lesson grants and revokes,
package coverage rules, the consumer's edge cases, the resync and startup."""

from datetime import datetime, timedelta, timezone

import access_events
import consumer
import database
import events
import jwt
import main
import pytest
from database import Base, get_db
from fastapi.testclient import TestClient
from main import app
from models import (
    ClassEnrollment,
    ClassPackage,
    Enrollment,
    EnrollmentStatus,
    LessonAccess,
    ReplicaCourse,
    ReplicaStream,
    ReplicaUser,
)
from package_access import (
    allowed_student_grades,
    course_access,
    grant_package,
    package_covers,
)
from services import activate_or_extend_enrollment, suspend_enrollment
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import auth
from shared import events as ev

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


client = TestClient(app)


def _auth(user_id: int, role: str) -> dict:
    token = jwt.encode(
        {"sub": str(user_id), "role": role}, auth.SECRET_KEY, algorithm=auth.ALGORITHM
    )
    return {"Authorization": f"Bearer {token}"}


STUDENT = _auth(101, "student")
ADMIN = _auth(1, "admin")
TEACHER = _auth(5, "teacher")
API = "/api/enrollments"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setitem(app.dependency_overrides, get_db, override_get_db)
    monkeypatch.setattr(consumer, "SessionLocal", TestingSessionLocal)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    published = []

    def capture(key, payload):
        published.append((key, payload))
        return True

    monkeypatch.setattr(access_events, "publish", capture)
    monkeypatch.setattr(events, "publish", capture)
    return published


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _access(sent, student_id):
    """The latest access.changed payload for one student."""
    return [
        p for k, p in sent if k == ev.ACCESS_CHANGED and p["student_id"] == student_id
    ][-1]


def _course(db, course_id=20, grade=11, status="PUBLISHED", streams=None):
    db.add(
        ReplicaCourse(
            id=course_id,
            title=f"C{course_id}",
            grade=grade,
            status=status,
            stream_ids=streams,
        )
    )
    db.commit()


def _package(db, grade=11, stream_id=None, price=0, active=True, name=None):
    pkg = ClassPackage(
        name=name or f"Grade {grade}",
        grade=grade,
        stream_id=stream_id,
        price=price,
        is_active=active,
    )
    db.add(pkg)
    db.commit()
    db.refresh(pkg)
    return pkg


def _enrollment(db, student_id=101, course_id=20, **fields):
    fields.setdefault("status", EnrollmentStatus.ACTIVE)
    e = Enrollment(student_id=student_id, course_id=course_id, **fields)
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


def _student(db, user_id, name, grade=11, school="Royal"):
    db.add(
        ReplicaUser(
            user_id=user_id, role="student", full_name=name, grade=grade, school=school
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def test_given_running_service_when_health_requested_then_reports_ok():
    # Act and assert
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "enrollment_service",
    }


def test_given_service_starting_when_lifespan_runs_then_tables_created_and_listeners_started(
    monkeypatch,
):
    # Arrange
    startup_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    calls = []
    monkeypatch.setattr(database, "engine", startup_engine)
    monkeypatch.setattr(consumer, "start_consumer", lambda: calls.append("consumer"))
    monkeypatch.setattr(
        main.messaging, "resync_later", lambda task: calls.append(("resync", task))
    )

    with TestClient(app):  # runs the lifespan; SQLite rejects the column migration
        pass

    # Act and assert
    with startup_engine.connect() as conn:
        assert "enrollments" in startup_engine.dialect.get_table_names(conn)

    # Act and assert
    assert calls == ["consumer", ("resync", main._resync)]
    startup_engine.dispose()


def test_given_schema_errors_when_lifespan_runs_then_service_still_starts(monkeypatch):
    # Arrange
    calls = []

    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(database.Base.metadata, "create_all", broken)
    monkeypatch.setattr(consumer, "start_consumer", lambda: calls.append("consumer"))
    monkeypatch.setattr(main.messaging, "resync_later", lambda task: None)
    with TestClient(app):
        pass

    # Act and assert
    assert calls == ["consumer"]


def test_given_existing_access_and_enrollments_when_resync_runs_then_all_are_announced(
    db, sent, monkeypatch
):
    # Arrange
    _course(db)
    pkg = _package(db)
    db.add(ClassEnrollment(user_id=300, class_package_id=pkg.id, status="active"))
    db.add(LessonAccess(student_id=302, course_id=21, module_id=7))
    db.commit()
    _enrollment(db, student_id=301)
    monkeypatch.setattr(main, "SessionLocal", TestingSessionLocal)

    main._resync()

    announced = [p["student_id"] for k, p in sent if k == ev.ACCESS_CHANGED]

    # Act and assert
    assert announced == [300, 301, 302]
    assert _access(sent, 300)["courses"][0]["id"] == 20
    assert _access(sent, 302)["lessons"] == [7]
    assert [p["source"] for k, p in sent if k == ev.ENROLLMENT_CHANGED] == ["resync"]


def test_given_consumer_when_started_then_binds_enrollment_queue(monkeypatch):
    # Arrange
    calls = []
    monkeypatch.setattr(
        consumer.messaging, "start_consumer", lambda *args: calls.append(args)
    )

    # Act
    consumer.start_consumer()

    # Assert
    assert calls == [
        (
            "enrollment",
            "enrollment.queue",
            ev.QUEUES["enrollment.queue"],
            consumer.handle,
        )
    ]


# ---------------------------------------------------------------------------
# Consumer edge cases
# ---------------------------------------------------------------------------


def test_given_incomplete_payment_and_expiry_events_when_consumed_then_they_are_ignored(
    db, sent
):
    # Arrange
    consumer.handle(ev.PAYMENT_SUCCESS, {"student_id": 101})

    consumer.handle(ev.SUBSCRIPTION_EXPIRED, {"course_id": 20})

    # Act and assert
    assert db.query(Enrollment).count() == 0 and sent == []


def test_given_expiry_event_without_enrollment_when_consumed_then_access_is_still_announced(
    db, sent
):
    # Act
    consumer.handle(ev.SUBSCRIPTION_EXPIRED, {"student_id": 101, "course_id": 20})

    # Assert
    assert [k for k, _ in sent] == [ev.ACCESS_CHANGED]


def test_given_user_and_stream_events_when_consumed_then_they_are_stored_in_their_copies(
    db,
):
    # Arrange
    consumer.handle(
        ev.USER_PROFILE_UPSERTED,
        {"user_id": 101, "full_name": "Ama", "role": "student"},
    )
    consumer.handle(ev.USER_STREAM_UPSERTED, {"id": 3, "name": "Commerce"})

    # Act and assert
    assert db.get(ReplicaUser, 101).full_name == "Ama"
    assert db.get(ReplicaStream, 3).name == "Commerce"


def test_given_course_deleted_when_event_consumed_then_package_holders_are_announced_again(
    db, sent
):
    # Arrange
    _course(db)
    pkg = _package(db)
    db.add(ClassEnrollment(user_id=300, class_package_id=pkg.id, status="active"))
    db.commit()

    consumer.handle(ev.COURSE_DELETED, {"id": 20})

    # Act and assert
    assert _access(sent, 300)["courses"] == []


def test_given_handler_error_when_event_processed_then_session_rolled_back_and_error_reraised(
    monkeypatch,
):
    # Arrange
    class FakeSession:
        rolled_back = closed = False

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    fake = FakeSession()

    def explode(*args, **kwargs):
        raise ValueError("bad event")

    monkeypatch.setattr(consumer, "SessionLocal", lambda: fake)
    monkeypatch.setattr(consumer.replicas, "apply", explode)

    # Act and assert
    with pytest.raises(ValueError):
        consumer.handle(ev.USER_PROFILE_UPSERTED, {})

    # Act and assert
    assert fake.rolled_back and fake.closed


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------


def test_given_lapsed_active_enrollment_when_payment_arrives_then_it_restarts_from_now(
    db,
):
    # Arrange
    _enrollment(db, expires_at=datetime.now(timezone.utc) - timedelta(days=10))
    renewed = activate_or_extend_enrollment(db, 101, 20)

    expires = renewed.expires_at.replace(tzinfo=timezone.utc)

    # Act and assert
    assert (
        timedelta(days=29) < expires - datetime.now(timezone.utc) <= timedelta(days=30)
    )


def test_given_suspended_enrollment_when_payment_arrives_then_it_is_reactivated(db):
    # Arrange
    _enrollment(db, status=EnrollmentStatus.SUSPENDED)

    renewed = activate_or_extend_enrollment(db, 101, 20, extension_days=7)

    # Act and assert
    assert renewed.status == EnrollmentStatus.ACTIVE
    assert db.query(Enrollment).count() == 1


def test_given_unknown_enrollment_when_suspended_then_none_is_returned(db):
    # Act and assert
    assert suspend_enrollment(db, 101, 20) is None


# ---------------------------------------------------------------------------
# Package coverage rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grade, expected",
    [(None, None), (10, [10, 11]), (12, [12, 13]), (9, [9]), (13, [13])],
)
def test_given_course_grade_band_when_allowed_grades_computed_then_expected_grades_are_returned(
    grade, expected
):
    # Act and assert
    assert allowed_student_grades(grade) == expected


@pytest.mark.parametrize(
    "pkg_grade, pkg_stream, course, covered",
    [
        (9, None, {"grade": 9}, True),
        (9, None, {"grade": 10}, False),
        (11, None, {"grade": 10}, True),
        (11, None, {"grade": 12}, False),
        (12, 1, {"grade": 12, "stream_ids": [1, 2]}, True),
        (12, 3, {"grade": 12, "stream_ids": [1, 2]}, False),
        (12, 3, {"grade": 12, "stream_ids": []}, True),  # open to every stream
        (12, None, {"grade": 12, "stream_ids": [1]}, True),
        (12, 1, {"grade": 13, "stream_ids": [1]}, False),
        (13, 1, {"grade": 12, "stream_ids": [1]}, True),
        (11, None, {"grade": None}, False),
        (4, None, {"grade": 4}, False),
    ],
)
def test_given_package_and_course_when_coverage_checked_then_result_matches_grade_and_stream(
    pkg_grade, pkg_stream, course, covered
):
    # Act and assert
    assert package_covers(pkg_grade, pkg_stream, course) is covered


def test_given_enrollment_package_and_lesson_grants_when_course_access_checked_then_each_path_gives_access(
    db,
):
    # Arrange
    _course(db, 20, grade=11)

    _course(db, 30, grade=12)

    # Act and assert
    assert course_access(db, 101, 20) == (False, None)

    pkg = _package(db, grade=11)

    grant_package(db, 101, pkg.id)
    db.commit()

    # Act and assert
    assert course_access(db, 101, 20) == (True, "class")
    assert course_access(db, 101, 30) == (False, None)
    assert course_access(db, 101, 99) == (False, None)  # unknown course

    _enrollment(db, course_id=30)

    # Act and assert
    assert course_access(db, 101, 30) == (True, "course")


def test_given_existing_package_row_when_package_granted_then_row_is_reactivated(db):
    # Arrange
    pkg = _package(db)
    row = grant_package(db, 101, pkg.id, payment_id=1)
    row.status = "cancelled"
    db.commit()
    until = datetime(2030, 1, 1, tzinfo=timezone.utc)

    again = grant_package(db, 101, pkg.id, payment_id=2, expires_at=until)
    db.commit()

    # Act and assert
    assert db.query(ClassEnrollment).count() == 1
    assert (again.status, again.payment_id) == ("active", 2)
    assert again.expires_at.replace(tzinfo=timezone.utc) == until


def test_given_direct_enrollment_and_package_when_access_checked_then_direct_enrollment_end_date_wins(
    db, sent
):
    # Arrange
    _course(db)
    pkg = _package(db)
    db.add(
        ClassEnrollment(
            user_id=101,
            class_package_id=pkg.id,
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(days=5),
        )
    )
    db.commit()
    _enrollment(db)

    access_events.announce_access(db, 101)

    # Act and assert
    assert _access(sent, 101)["courses"] == [{"id": 20, "until": None}]


# ---------------------------------------------------------------------------
# A student's own enrollments and access
# ---------------------------------------------------------------------------


def test_given_student_with_lesson_grants_when_listing_my_enrollments_then_grants_are_included(
    db,
):
    # Arrange
    _enrollment(db, course_id=20)
    _enrollment(db, course_id=21, is_manual=True)
    db.add(LessonAccess(student_id=101, course_id=22, module_id=9))
    db.add(LessonAccess(student_id=999, course_id=22, module_id=9))
    db.commit()

    # Act
    rows = client.get(f"{API}/me", headers=STUDENT).json()

    # Assert
    assert sorted(r["course_id"] for r in rows) == [20, 21, 22]

    # Act
    lesson = next(r for r in rows if r["course_id"] == 22)

    # Assert
    assert (lesson["module_id"], lesson["is_manual"]) == (9, True)

    # Act
    only = client.get(f"{API}/me?course_id=22", headers=STUDENT).json()

    # Assert
    assert [r["module_id"] for r in only] == [9]

    # Act and assert
    assert client.get(f"{API}/me").status_code == 401


def test_given_student_when_my_course_access_requested_then_access_details_returned(db):
    # Arrange
    db.add(LessonAccess(student_id=101, course_id=20, module_id=9))
    db.commit()

    # Act and assert
    assert client.get(f"{API}/me/access?course_id=20", headers=STUDENT).json() == {
        "course": False,
        "via": None,
        "lessons": [9],
    }
    _enrollment(db)

    # Act and assert
    assert client.get(f"{API}/me/access?course_id=20", headers=STUDENT).json() == {
        "course": True,
        "via": "course",
        "lessons": [],
    }


def test_given_admin_when_enrollment_requested_then_enrollment_is_returned(db):
    # Arrange
    _enrollment(db)

    # Act
    resp = client.get(f"{API}?student_id=101&course_id=20", headers=ADMIN)

    # Assert
    assert resp.status_code == 200 and resp.json()["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Class packages: browsing and free enrollment
# ---------------------------------------------------------------------------


def test_given_packages_when_listed_by_grade_and_stream_then_only_matching_packages_returned(
    db,
):
    # Arrange
    db.add(ReplicaStream(id=1, name="Bio", is_active=True))
    db.commit()
    _package(db, grade=11, name="G11")
    _package(db, grade=12, stream_id=1, name="Bio 12")
    _package(db, grade=12, stream_id=2, name="Maths 12")
    _package(db, grade=12, stream_id=None, name="Any 12")
    _package(db, grade=10, active=False, name="Old")

    def names(query):
        return [p["name"] for p in client.get(f"{API}/class-packages{query}").json()]

    # Act and assert
    assert names("") == ["G11", "Any 12", "Bio 12", "Maths 12"]
    assert names("?grade=12&stream_id=1") == ["Any 12", "Bio 12"]
    assert names("?grade=11") == ["G11"]

    # Act
    bio = next(
        p
        for p in client.get(f"{API}/class-packages?grade=12&stream_id=1").json()
        if p["name"] == "Bio 12"
    )

    # Assert
    assert (bio["stream_name"], bio["stream_id"], bio["is_enrolled"]) == (
        "Bio",
        "1",
        False,
    )


def test_given_student_with_package_when_packages_listed_then_own_package_is_marked(db):
    # Arrange
    pkg = _package(db)
    db.add(ClassEnrollment(user_id=101, class_package_id=pkg.id, status="active"))
    db.commit()

    # Act
    (row,) = client.get(f"{API}/class-packages", headers=STUDENT).json()

    # Assert
    assert row["is_enrolled"] is True


def test_given_free_package_when_student_enrolls_then_enrolled_and_second_attempt_is_already_enrolled(
    db, sent
):
    # Arrange
    _course(db)
    pkg = _package(db)

    # Act
    resp = client.post(f"{API}/class-packages/{pkg.id}/enroll-free", headers=STUDENT)

    # Assert
    assert resp.status_code == 200
    assert resp.json()["status"] == "active" and resp.json()["expires_at"]
    assert _access(sent, 101)["courses"][0]["id"] == 20

    # Act
    again = client.post(f"{API}/class-packages/{pkg.id}/enroll-free", headers=STUDENT)

    # Assert
    assert again.json()["detail"]["error"] == "ALREADY_ENROLLED"


def test_given_inactive_unknown_or_paid_package_when_enrolling_for_free_then_each_is_rejected(
    db,
):
    # Arrange
    paid = _package(db, price=5000)
    inactive = _package(db, active=False)
    url = f"{API}/class-packages/{{}}/enroll-free"

    # Act and assert
    assert (
        client.post(url.format(paid.id), headers=STUDENT).json()["detail"]["error"]
        == "PAYMENT_REQUIRED"
    )
    assert client.post(url.format(inactive.id), headers=STUDENT).status_code == 404
    assert client.post(url.format(999), headers=STUDENT).status_code == 404
    assert client.post(url.format(paid.id), headers=ADMIN).status_code == 403


def test_given_student_with_class_enrollments_when_listed_then_own_enrollments_returned(
    db,
):
    # Arrange
    db.add(ReplicaStream(id=1, name="Bio", is_active=True))
    db.commit()
    bio = _package(db, grade=12, stream_id=1, name="Bio 12")
    general = _package(db, grade=11, name="G11")
    old = _package(db, grade=10, name="Old")
    for pkg, status in ((bio, "active"), (general, "active"), (old, "cancelled")):
        db.add(ClassEnrollment(user_id=101, class_package_id=pkg.id, status=status))
    db.commit()

    # Act
    rows = client.get(f"{API}/my-class-enrollments", headers=STUDENT).json()
    by_name = {r["package_name"]: r for r in rows}

    # Assert
    assert set(by_name) == {"Bio 12", "G11"}
    assert by_name["Bio 12"]["package_stream_name"] == "Bio"
    assert by_name["G11"]["package_stream_name"] is None


# ---------------------------------------------------------------------------
# Class packages: admin
# ---------------------------------------------------------------------------


def test_given_admin_when_creating_packages_then_they_are_created_and_can_be_filtered(
    db,
):
    # Act
    resp = client.post(
        f"{API}/admin/class-packages",
        json={"name": "Grade 9", "grade": 9, "price": 1500},
        headers=ADMIN,
    )

    # Assert
    assert resp.status_code == 201 and resp.json()["price"] == 1500.0
    _package(db, grade=12, stream_id=2, name="Bio")
    _package(db, grade=9, active=False, name="Old 9")

    def names(query):
        url = f"{API}/admin/class-packages{query}"
        return [p["name"] for p in client.get(url, headers=ADMIN).json()]

    # Act and assert
    assert names("") == ["Grade 9", "Old 9", "Bio"]
    assert names("?grade=9&is_active=true") == ["Grade 9"]
    assert names("?stream_id=2") == ["Bio"]
    assert names("?is_active=false") == ["Old 9"]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "grade": 9},
        {"name": "x", "grade": 4},
        {"name": "x", "grade": 9, "price": -1},
    ],
)
def test_given_invalid_package_when_created_then_422(body):
    # Act and assert
    assert (
        client.post(f"{API}/admin/class-packages", json=body, headers=ADMIN).status_code
        == 422
    )


def test_given_non_admin_or_no_token_when_calling_package_admin_routes_then_403_or_401():
    # Act and assert
    assert client.get(f"{API}/admin/class-packages", headers=TEACHER).status_code == 403
    assert client.post(f"{API}/admin/class-packages/auto-generate").status_code == 401


def test_given_missing_package_when_updated_or_deleted_then_404_package_not_found():
    # Act and assert
    assert (
        client.patch(f"{API}/admin/class-packages/9", json={}, headers=ADMIN).json()[
            "detail"
        ]["error"]
        == "PACKAGE_NOT_FOUND"
    )
    assert (
        client.delete(f"{API}/admin/class-packages/9", headers=ADMIN).status_code == 404
    )


def test_given_package_with_holders_when_deleted_then_holders_removed_and_announced_again(
    db, sent
):
    # Arrange
    _course(db)
    pkg = _package(db)
    db.add(ClassEnrollment(user_id=101, class_package_id=pkg.id, status="active"))
    db.commit()

    access_events.announce_access(db, 101)

    # Act and assert
    assert _access(sent, 101)["courses"]

    # Act
    resp = client.delete(f"{API}/admin/class-packages/{pkg.id}", headers=ADMIN)

    # Assert
    assert resp.json() == {"status": "success"}
    assert db.query(ClassEnrollment).count() == 0
    assert _access(sent, 101)["courses"] == []


def test_given_class_enrollments_when_admin_lists_then_names_are_included_and_user_filter_works(
    db,
):
    # Arrange
    _student(db, 101, "Ama Perera")
    a = _package(db, name="A")
    b = _package(db, name="B")
    db.add(ClassEnrollment(user_id=101, class_package_id=a.id, payment_id=7))
    db.add(ClassEnrollment(user_id=102, class_package_id=b.id))
    db.commit()

    # Act
    rows = client.get(f"{API}/admin/class-enrollments", headers=ADMIN).json()
    by_user = {r["user_id"]: r for r in rows}

    # Assert
    assert by_user["101"]["user_email"] == "Ama Perera"  # display name from the copy
    assert by_user["101"]["payment_id"] == "7"
    assert by_user["102"]["user_email"] is None and by_user["102"]["payment_id"] is None

    # Act
    filtered = client.get(
        f"{API}/admin/class-enrollments?user_id=102&class_package_id={b.id}",
        headers=ADMIN,
    ).json()

    # Assert
    assert [r["user_id"] for r in filtered] == ["102"]


def test_given_admin_when_granting_package_with_end_date_then_class_enrollment_is_created(
    db, sent
):
    # Arrange
    pkg = _package(db)

    # Act
    resp = client.post(
        f"{API}/admin/class-enrollments",
        json={
            "user_id": 101,
            "class_package_id": pkg.id,
            "expires_at": "2030-01-01T00:00:00Z",
        },
        headers=ADMIN,
    )

    # Assert
    assert resp.status_code == 201
    assert resp.json()["expires_at"].startswith("2030-01-01")
    assert _access(sent, 101) is not None

    # Act
    missing = client.post(
        f"{API}/admin/class-enrollments",
        json={"user_id": 101, "class_package_id": 999},
        headers=ADMIN,
    )

    # Assert
    assert missing.status_code == 404


def test_given_class_enrollment_when_admin_revokes_then_it_is_removed_and_unknown_one_is_404(
    db, sent
):
    # Arrange
    pkg = _package(db)
    row = ClassEnrollment(user_id=101, class_package_id=pkg.id)
    db.add(row)
    db.commit()

    # Act
    resp = client.delete(f"{API}/admin/class-enrollments/{row.id}", headers=ADMIN)

    # Assert
    assert resp.json() == {"status": "success"}
    assert _access(sent, 101)["courses"] == []

    # Act
    again = client.delete(f"{API}/admin/class-enrollments/{row.id}", headers=ADMIN)

    # Assert
    assert again.json()["detail"]["error"] == "CLASS_ENROLLMENT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Manual access: whole course or one lesson
# ---------------------------------------------------------------------------


def test_given_cancelled_enrollment_when_admin_grants_then_enrollment_is_reactivated(
    db, sent
):
    # Arrange
    _course(db)
    old = _enrollment(db, status=EnrollmentStatus.CANCELLED)

    # Act
    resp = client.post(
        f"{API}/admin/grant",
        json={"course_id": 20, "user_ids": [101, 101]},
        headers=ADMIN,
    )

    # Assert
    assert resp.json() == {"message": "Access granted to 1 student(s)", "granted": 1}
    db.refresh(old)

    # Act and assert
    assert old.status == EnrollmentStatus.ACTIVE and old.is_manual is True
    assert old.expires_at is None
    assert [p["source"] for k, p in sent if k == ev.ENROLLMENT_CHANGED] == ["manual"]


def test_given_students_with_access_when_admin_grants_then_they_are_skipped(db):
    # Arrange
    _course(db)
    _enrollment(db)

    # Act
    resp = client.post(
        f"{API}/admin/grant", json={"course_id": 20, "user_ids": [101]}, headers=ADMIN
    )

    # Assert
    assert resp.json()["granted"] == 0


def test_given_admin_when_granting_a_single_lesson_then_only_that_lesson_is_opened(
    db, sent
):
    # Arrange
    _course(db)
    body = {"course_id": 20, "module_id": 9, "user_ids": [101, 102]}

    # Act and assert
    assert (
        client.post(f"{API}/admin/grant", json=body, headers=ADMIN).json()["granted"]
        == 2
    )
    assert (
        client.post(f"{API}/admin/grant", json=body, headers=ADMIN).json()["granted"]
        == 0
    )

    row = db.query(LessonAccess).filter_by(student_id=101).one()

    # Act and assert
    assert (row.module_id, row.granted_by) == (9, 1)
    assert _access(sent, 102)["lessons"] == [9]
    assert not [k for k, _ in sent if k == ev.ENROLLMENT_CHANGED]


def test_given_invalid_grant_request_when_granting_then_422_or_course_not_found(db):
    # Act and assert
    assert (
        client.post(
            f"{API}/admin/grant", json={"course_id": 99, "user_ids": [1]}, headers=ADMIN
        ).json()["detail"]["error"]
        == "COURSE_NOT_FOUND"
    )
    _course(db)

    # Act and assert
    assert (
        client.post(
            f"{API}/admin/grant", json={"course_id": 20, "user_ids": []}, headers=ADMIN
        ).status_code
        == 422
    )


def test_given_enrolled_student_when_course_revoked_then_record_is_kept_and_unknown_is_404(
    db, sent
):
    # Arrange
    enrollment = _enrollment(db)

    # Act
    resp = client.delete(f"{API}/admin/revoke?course_id=20&user_id=101", headers=ADMIN)

    # Assert
    assert resp.json() == {"message": "Access revoked"}
    db.refresh(enrollment)

    # Act and assert
    assert enrollment.status == EnrollmentStatus.CANCELLED
    assert [k for k, _ in sent] == [ev.ENROLLMENT_CHANGED, ev.ACCESS_CHANGED]

    # Act
    again = client.delete(f"{API}/admin/revoke?course_id=20&user_id=101", headers=ADMIN)

    # Assert
    assert again.json()["detail"]["error"] == "ENROLLMENT_NOT_FOUND"


def test_given_lesson_grant_when_revoked_then_access_removed_and_unknown_grant_is_404(
    db, sent
):
    # Arrange
    db.add(LessonAccess(student_id=101, course_id=20, module_id=9))
    db.commit()
    url = f"{API}/admin/revoke?course_id=20&user_id=101&module_id=9"

    # Act and assert
    assert client.delete(url, headers=ADMIN).status_code == 200

    # Act and assert
    assert db.query(LessonAccess).count() == 0

    # Act and assert
    assert (
        client.delete(url, headers=ADMIN).json()["detail"]["error"]
        == "ACCESS_NOT_FOUND"
    )


def test_given_lesson_grants_when_enrolled_list_requested_then_grants_are_included(db):
    # Arrange
    _student(db, 101, "Ama")
    _enrollment(db, enrolled_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    db.add(LessonAccess(student_id=102, course_id=20, module_id=9))
    db.add(LessonAccess(student_id=101, course_id=20, module_id=9))  # already in
    db.commit()

    # Act
    whole = client.get(f"{API}/admin/enrolled?course_id=20", headers=ADMIN).json()

    # Assert
    assert [(s["user_id"], s["full_name"]) for s in whole] == [("101", "Ama")]

    # Act
    with_lesson = client.get(
        f"{API}/admin/enrolled?course_id=20&module_id=9", headers=ADMIN
    ).json()
    names = {s["user_id"]: (s["full_name"], s["is_manual"]) for s in with_lesson}

    # Assert
    assert names == {"101": ("Ama", False), "102": ("User #102", True)}


def test_given_unknown_course_when_eligible_students_requested_then_404_course_not_found():
    # Act
    resp = client.get(f"{API}/admin/eligible?course_id=99", headers=ADMIN)

    # Assert
    assert resp.json()["detail"]["error"] == "COURSE_NOT_FOUND"


def test_given_search_text_when_eligible_students_requested_then_matching_schools_are_returned(
    db,
):
    # Arrange
    _course(db, grade=12)
    _student(db, 101, "Ama", grade=13, school="Royal")
    _student(db, 102, "Kamal", grade=12, school="Ananda")
    _student(db, 103, "Nimal", grade=11, school="Royal")

    # Act
    resp = client.get(
        f"{API}/admin/eligible?course_id=20&search=royal", headers=ADMIN
    ).json()

    # Assert
    assert [s["full_name"] for s in resp] == ["Ama"]


def test_given_no_ids_when_display_names_requested_then_empty_result_returned(db):
    # Arrange
    import router

    # Act and assert
    assert router.display_names(db, []) == {}


# ---------------------------------------------------------------------------
# Event payloads
# ---------------------------------------------------------------------------


def test_given_activated_enrollment_when_event_payload_built_then_it_carries_the_source_and_expiry(
    sent,
):
    # Act
    events.publish_enrollment_activated(1, 101, 20, None, source="manual")
    payload = sent[0][1]

    # Assert
    assert (payload["source"], payload["expires_at"]) == ("manual", None)
