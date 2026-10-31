"""Course service on SQLite: catalogue browsing and visibility, authoring of courses,
lessons, videos and materials, paid-content gating, in-video checkpoints, events,
startup and the access-list consumer. test_courses.py covers the same routes
against Postgres when DATABASE_URL is set; these run everywhere."""

import asyncio
import json
import os
from decimal import Decimal

# test_courses.py signs its tokens with this key and sets it before importing the
# app; shared.auth reads it once, so whichever file is imported first must agree.
os.environ.setdefault("SECRET_KEY", "test-secret-for-course-service")

import access
import event_consumer
import events
import jwt
import main
import pytest
from database import Base, get_db
from fastapi.testclient import TestClient
from main import app
from models import (
    CheckpointQuestion,
    Course,
    CourseStatus,
    CourseType,
    Lesson,
    Material,
    Module,
    ReplicaAccess,
    VideoCheckpoint,
)
from router import _parse_timestamp, get_allowed_grades_for_student
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
    # shared.auth reads SECRET_KEY once; test_courses.py may have changed it first.
    token = jwt.encode(
        {"sub": str(user_id), "role": role}, auth.SECRET_KEY, algorithm=auth.ALGORITHM
    )
    return {"Authorization": f"Bearer {token}"}


OWNER = _auth(5, "teacher")
OTHER_TEACHER = _auth(6, "teacher")
ADMIN = _auth(1, "admin")
STUDENT = _auth(101, "student")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setitem(app.dependency_overrides, get_db, override_get_db)
    for module in (access, event_consumer):
        monkeypatch.setattr(module, "SessionLocal", TestingSessionLocal)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    published = []
    monkeypatch.setattr(
        events, "publish", lambda key, payload: published.append((key, payload)) or True
    )
    return published


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _course(db, **fields) -> Course:
    fields.setdefault("instructor_id", 5)
    fields.setdefault("title", "Python")
    fields.setdefault("course_type", CourseType.video)
    fields.setdefault("status", CourseStatus.PUBLISHED)
    course = Course(**fields)
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


def _module(db, course_id, **fields) -> Module:
    fields.setdefault("title", "Lesson")
    m = Module(course_id=course_id, **fields)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _video(db, module: Module, **fields) -> Lesson:
    fields.setdefault("title", "Video")
    fields.setdefault("youtube_video_id", "yt123")
    lesson = Lesson(module_id=module.id, course_id=module.course_id, **fields)
    db.add(lesson)
    db.commit()
    db.refresh(lesson)
    return lesson


def _material(db, module: Module, **fields) -> Material:
    fields.setdefault("title", "Notes")
    fields.setdefault("file_url", "https://files/notes.pdf")
    fields.setdefault("file_type", "pdf")
    m = Material(course_id=module.course_id, module_id=module.id, **fields)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _grant(db, student_id=101, courses=(), lessons=()):
    db.add(
        ReplicaAccess(
            student_id=student_id,
            courses=[{"id": c, "until": None} for c in courses],
            lessons=list(lessons),
        )
    )
    db.commit()


@pytest.fixture
def tree(db):
    """Published course 1 (owner 5) → lesson module → one video."""
    course = _course(db)
    module = _module(db, course.id)
    video = _video(db, module)
    return course, module, video


def _base(course, module):
    return f"/{course.id}/modules/{module.id}"


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def test_given_running_service_when_health_requested_then_reports_ok():
    # Act and assert
    assert client.get("/health").json() == {"status": "ok", "service": "course_service"}


@pytest.fixture
def startup(monkeypatch):
    startup_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    calls = []
    monkeypatch.setattr(main, "engine", startup_engine)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("consumer"))
    monkeypatch.setattr(
        main.messaging, "resync_later", lambda task: calls.append(("resync", task))
    )
    yield startup_engine, calls
    startup_engine.dispose()


def test_given_service_starting_when_startup_runs_then_tables_created_and_listeners_started(
    startup,
):
    # Arrange
    startup_engine, calls = startup

    main.on_startup()  # SQLite rejects ADD COLUMN IF NOT EXISTS; logged, not fatal

    # Act and assert
    assert "courses" in startup_engine.dialect.get_table_names(startup_engine.connect())
    assert calls == ["consumer", ("resync", main._resync)]


def test_given_table_creation_fails_when_startup_runs_then_service_still_starts(
    startup, monkeypatch
):
    # Arrange
    _, calls = startup

    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)

    # Act
    main.on_startup()

    # Assert
    assert calls[0] == "consumer"


def test_given_existing_courses_and_lessons_when_resync_runs_then_each_is_announced_again(
    db, sent, monkeypatch
):
    # Arrange
    course = _course(db, stream_ids=[2], price=Decimal("1500"))

    module = _module(db, course.id, position=3)
    monkeypatch.setattr(main, "SessionLocal", TestingSessionLocal)

    main._resync()

    # Act and assert
    assert sent == [
        (ev.COURSE_UPSERTED, events.course_event(course)),
        (ev.COURSE_MODULE_UPSERTED, events.module_event(module)),
    ]
    assert sent[0][1]["price"] == 1500.0 and sent[0][1]["status"] == "PUBLISHED"


def test_given_access_event_when_consumed_then_access_is_stored_and_consumer_binds_its_queue(
    db, monkeypatch
):
    # Arrange
    event_consumer.handle(
        "access.changed",
        {"student_id": 101, "courses": [{"id": 1, "until": None}], "lessons": [9]},
    )

    # Act and assert
    assert asyncio.run(access.is_enrolled(101, 1)) is True
    assert asyncio.run(access.is_enrolled(101, 2, 9)) is True
    assert asyncio.run(access.is_enrolled(101, 2, 8)) is False

    calls = []
    monkeypatch.setattr(
        event_consumer.messaging, "start_consumer", lambda *args: calls.append(args)
    )

    # Act
    event_consumer.start()

    # Assert
    assert calls == [
        ("course", "course.queue", ev.QUEUES["course.queue"], event_consumer.handle)
    ]


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

    monkeypatch.setattr(event_consumer, "SessionLocal", lambda: fake)
    monkeypatch.setattr(event_consumer.replicas, "apply", explode)

    # Act and assert
    with pytest.raises(ValueError):
        event_consumer.handle("access.changed", {})

    # Act and assert
    assert fake.rolled_back and fake.closed


# ---------------------------------------------------------------------------
# Browsing the catalogue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grade, allowed", [(11, [10, 11]), (13, [12, 13]), (10, [10]), (12, [12])]
)
def test_given_student_grade_when_allowed_course_grades_requested_then_matching_grades_returned(
    grade, allowed
):
    # Act and assert
    assert get_allowed_grades_for_student(grade) == allowed


def test_given_draft_and_published_courses_when_public_lists_courses_then_only_published_are_returned(
    db,
):
    _course(db, title="Live")
    _course(db, title="Draft", status=CourseStatus.DRAFT)
    _course(db, title="Old", status=CourseStatus.ARCHIVED)

    for headers in ({}, STUDENT):
        data = client.get("/", headers=headers).json()
        assert [c["title"] for c in data["items"]] == ["Live"]
        assert data["total"] == 1


def test_given_staff_when_listing_courses_by_status_then_filter_applies_and_invalid_status_is_422(
    db,
):
    # Arrange
    _course(db, title="Live")
    _course(db, title="Draft", status=CourseStatus.DRAFT)

    # Act and assert
    assert client.get("/", headers=ADMIN).json()["total"] == 2

    # Act
    drafts = client.get("/?status=DRAFT", headers=OWNER).json()["items"]

    # Assert
    assert [c["title"] for c in drafts] == ["Draft"]

    # Act
    resp = client.get("/?status=HIDDEN", headers=ADMIN)

    # Assert
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "INVALID_STATUS"


def test_given_grade_filter_when_listing_courses_then_ungraded_courses_are_kept(db):
    # Arrange
    for g in (10, 11, 12, None):
        _course(db, title=f"G{g}", grade=g)

    # Act
    titles = [c["title"] for c in client.get("/?grade=11").json()["items"]]

    # Assert
    assert titles == ["G10", "G11", "GNone"]


def test_given_stream_filter_when_listing_courses_then_it_applies_only_for_advanced_level_grades(
    db,
):
    # Arrange
    _course(db, title="Physics", grade=12, stream_ids=[1, 2])
    _course(db, title="Biology", grade=12, stream_ids=[2])
    _course(db, title="Maths", grade=12, stream_ids=[1])
    _course(db, title="General", grade=12, stream_ids=None)

    # Act
    resp = client.get("/?grade=12&stream_id=2").json()

    # Assert
    assert [c["title"] for c in resp["items"]] == ["Physics", "Biology", "General"]
    assert resp["total"] == 3

    # Act
    page = client.get("/?grade=12&stream_id=2&page=2&page_size=2").json()

    # Assert
    assert [c["title"] for c in page["items"]] == ["General"] and page["total"] == 3
    # Below A/L the stream is ignored.
    _course(db, title="O/L", grade=10, stream_ids=[9])

    # Act and assert
    assert client.get("/?grade=10&stream_id=2").json()["total"] == 1


def test_given_paging_parameters_when_listing_courses_then_pages_are_returned_and_limits_enforced(
    db,
):
    # Arrange
    for i in range(3):
        _course(db, title=f"C{i}")

    # Act
    page = client.get("/?page=2&page_size=2").json()

    # Assert
    assert (page["total"], page["page"], [c["title"] for c in page["items"]]) == (
        3,
        2,
        ["C2"],
    )

    # Act and assert
    assert client.get("/?page=0").status_code == 422
    assert client.get("/?page_size=101").status_code == 422


def test_given_inactive_lessons_when_student_lists_courses_with_modules_then_inactive_lessons_are_hidden(
    db,
):
    # Arrange
    course = _course(db)
    active = _module(db, course.id, title="Active", position=1)
    _module(db, course.id, title="Hidden", position=2, is_active=False)
    _video(db, active)
    _video(db, active)

    # Act
    item = client.get("/?include_modules=true").json()["items"][0]

    # Assert
    assert [(m["title"], m["lesson_count"]) for m in item["modules"]] == [("Active", 2)]

    # Act
    staff = client.get("/?include_modules=true", headers=OWNER).json()["items"][0]

    # Assert
    assert [m["title"] for m in staff["modules"]] == ["Active", "Hidden"]
    # Without the flag, modules are null (not "no modules").
    # Act and assert
    assert client.get("/").json()["items"][0]["modules"] is None


def test_given_no_courses_when_listing_with_modules_then_empty_list_returned():
    # Act and assert
    assert client.get("/?include_modules=true").json()["items"] == []


def test_given_course_without_modules_when_fetched_with_modules_then_module_list_is_empty(
    db,
):
    # Arrange
    course = _course(db)

    # Act and assert
    assert client.get(f"/{course.id}?include_modules=true").json()["modules"] == []


def test_given_draft_course_when_fetched_then_visibility_rules_apply_and_unknown_course_is_404(
    db,
):
    # Arrange
    draft = _course(db, status=CourseStatus.DRAFT)

    # Act and assert
    assert client.get(f"/{draft.id}").status_code == 404
    assert client.get(f"/{draft.id}", headers=STUDENT).status_code == 404
    assert client.get(f"/{draft.id}", headers=OWNER).status_code == 200

    # Act
    resp = client.get("/999")

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "COURSE_NOT_FOUND"


def test_given_modules_out_of_order_when_listed_then_sorted_by_position_and_draft_course_is_404(
    db,
):
    # Arrange
    course = _course(db)
    _module(db, course.id, title="B", position=2)
    _module(db, course.id, title="A", position=1)

    # Act and assert
    assert [m["title"] for m in client.get(f"/{course.id}/modules").json()] == [
        "A",
        "B",
    ]
    draft = _course(db, status=CourseStatus.DRAFT)

    # Act and assert
    assert client.get(f"/{draft.id}/modules").status_code == 404


# ---------------------------------------------------------------------------
# Course authoring
# ---------------------------------------------------------------------------


def test_given_teacher_when_creating_course_then_course_is_created_as_draft(sent):
    # Act
    resp = client.post(
        "/",
        json={
            "title": "Chemistry",
            "price": "2500.00",
            "course_type": "exam",
            "grade": 12,
            "stream_ids": [1, 3],
        },
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 201

    # Act
    data = resp.json()

    # Assert
    assert (data["status"], data["teacher_id"], data["course_type"]) == (
        "DRAFT",
        5,
        "exam",
    )
    assert data["stream_ids"] == [1, 3]
    key, payload = sent[-1]

    # Act and assert
    assert key == ev.COURSE_UPSERTED and payload["price"] == 2500.0


@pytest.mark.parametrize(
    "body",
    [
        {"title": "x", "course_type": "audio"},
        {"title": "x", "grade": 4},
        {"title": "x", "grade": 14},
        {},
    ],
)
def test_given_invalid_course_when_creating_then_422(body):
    # Act and assert
    assert client.post("/", json=body, headers=OWNER).status_code == 422


def test_given_no_token_or_student_when_creating_course_then_401_or_403():
    # Act and assert
    assert client.post("/", json={"title": "x"}).status_code == 401
    assert client.post("/", json={"title": "x"}, headers=STUDENT).status_code == 403


def test_given_owner_when_updating_course_fields_then_changes_are_saved(db, sent):
    # Arrange
    course = _course(db, status=CourseStatus.DRAFT)

    # Act
    resp = client.put(
        f"/{course.id}",
        json={"title": "Renamed", "price": "99.50", "course_type": "exam"},
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 200

    data = resp.json()

    # Act and assert
    assert (data["title"], data["course_type"], Decimal(data["price"])) == (
        "Renamed",
        "exam",
        Decimal("99.50"),
    )
    assert [k for k, _ in sent] == [ev.COURSE_UPSERTED]


@pytest.mark.parametrize(
    "body",
    [{"status": "LIVE"}, {"course_type": "audio"}, {"grade": 20}],
)
def test_given_invalid_fields_when_updating_course_then_422(db, body):
    # Arrange
    course = _course(db)

    # Act and assert
    assert client.put(f"/{course.id}", json=body, headers=OWNER).status_code == 422


def test_given_video_course_without_video_when_publishing_then_422_and_with_video_then_published(
    db, sent
):
    # Arrange
    course = _course(db, status=CourseStatus.DRAFT)
    module = _module(db, course.id)

    # Act
    resp = client.put(f"/{course.id}", json={"status": "PUBLISHED"}, headers=OWNER)

    # Assert
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "PUBLISH_REQUIRES_CONTENT"
    assert "video" in resp.json()["detail"]["message"]

    _video(db, module)

    # Act
    resp = client.put(f"/{course.id}", json={"status": "PUBLISHED"}, headers=OWNER)

    # Assert
    assert resp.status_code == 200 and resp.json()["status"] == "PUBLISHED"
    db.refresh(course)

    # Act and assert
    assert course.is_published is True
    assert sent[-1] == (
        ev.COURSE_PUBLISHED,
        {"course_id": course.id, "teacher_id": 5, "title": "Python"},
    )


def test_given_exam_course_when_publishing_then_a_lesson_is_enough(db):
    # Arrange
    course = _course(db, status=CourseStatus.DRAFT, course_type=CourseType.exam)

    # Act
    resp = client.put(f"/{course.id}", json={"status": "PUBLISHED"}, headers=OWNER)

    # Assert
    assert resp.status_code == 422
    assert (
        resp.json()["detail"]["message"] == "Add at least one lesson before publishing"
    )

    _module(db, course.id)

    # Act and assert
    assert (
        client.put(
            f"/{course.id}", json={"status": "PUBLISHED"}, headers=OWNER
        ).status_code
        == 200
    )


def test_given_course_switched_to_exam_type_when_publishing_then_new_type_rules_apply(
    db,
):
    # Arrange
    course = _course(db, status=CourseStatus.DRAFT)
    _module(db, course.id)  # a lesson, but no video

    # Act
    resp = client.put(
        f"/{course.id}",
        json={"status": "PUBLISHED", "course_type": "exam"},
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 200


def test_given_published_course_when_archived_then_it_is_unpublished(db, sent):
    # Arrange
    course = _course(db, is_published=True)

    # Act
    resp = client.put(f"/{course.id}", json={"status": "ARCHIVED"}, headers=OWNER)

    # Assert
    assert resp.json()["status"] == "ARCHIVED"
    db.refresh(course)

    # Act and assert
    assert course.is_published is False
    assert ev.COURSE_PUBLISHED not in [k for k, _ in sent]


def test_given_other_teacher_or_missing_course_when_updating_or_deleting_then_403_or_404(
    db,
):
    # Arrange
    course = _course(db)

    # Act and assert
    assert (
        client.put(f"/{course.id}", json={"title": "x"}, headers=OTHER_TEACHER).json()[
            "detail"
        ]["error"]
        == "NOT_COURSE_OWNER"
    )
    assert (
        client.put(f"/{course.id}", json={"title": "x"}, headers=ADMIN).status_code
        == 200
    )
    assert client.put("/999", json={"title": "x"}, headers=ADMIN).status_code == 404
    assert client.delete(f"/{course.id}", headers=OTHER_TEACHER).status_code == 403


def test_given_course_with_content_when_deleted_then_everything_under_it_is_removed(
    db, sent, tree
):
    # Arrange
    course, module, video = tree
    other = _course(db, title="Other")
    other_module = _module(db, other.id)
    _video(db, other_module)
    _material(db, module, lesson_id=video.id)
    cp = VideoCheckpoint(course_id=course.id, lesson_id=video.id, timestamp_seconds=5)
    db.add(cp)
    db.commit()
    db.add(
        CheckpointQuestion(
            checkpoint_id=cp.id,
            question_text="?",
            option_a="a",
            option_b="b",
            correct_option="a",
        )
    )
    db.commit()
    course_id, module_id = course.id, module.id

    # Act and assert
    assert client.delete(f"/{course_id}", headers=OWNER).status_code == 204

    db.expire_all()

    # Act and assert
    assert db.get(Course, course_id) is None

    # Act and assert
    assert db.query(Module).filter_by(course_id=course_id).count() == 0
    assert db.query(Lesson).count() == 1  # the other course's video
    assert db.query(Material).count() == 0
    assert db.query(VideoCheckpoint).count() == 0
    assert db.query(CheckpointQuestion).count() == 0
    assert sent == [
        (ev.COURSE_MODULE_DELETED, {"id": module_id}),
        (ev.COURSE_DELETED, {"id": course_id}),
    ]


# ---------------------------------------------------------------------------
# Lessons (modules)
# ---------------------------------------------------------------------------


def test_given_course_owner_when_creating_module_then_module_is_created(db, sent):
    # Arrange
    course = _course(db)

    # Act
    resp = client.post(
        f"/{course.id}/modules",
        json={"title": "Algebra", "order": 2, "is_active": False},
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 201

    # Act
    data = resp.json()

    # Assert
    assert (data["order"], data["is_active"], data["lesson_count"]) == (2, False, 0)
    assert sent[-1][0] == ev.COURSE_MODULE_UPSERTED
    assert sent[-1][1]["is_active"] is False


def test_given_non_owner_or_missing_module_when_changing_modules_then_403_or_404(db):
    # Arrange
    course = _course(db)

    # Act and assert
    assert (
        client.post(
            f"/{course.id}/modules", json={"title": "x"}, headers=OTHER_TEACHER
        ).status_code
        == 403
    )
    assert (
        client.post("/999/modules", json={"title": "x"}, headers=ADMIN).status_code
        == 404
    )

    # Act
    resp = client.put(f"/{course.id}/modules/999", json={}, headers=OWNER)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "MODULE_NOT_FOUND"


def test_given_module_of_another_course_when_updating_then_404(db):
    # Arrange
    a = _course(db)
    b = _course(db)
    module_of_b = _module(db, b.id)

    # Act
    resp = client.put(f"/{a.id}/modules/{module_of_b.id}", json={}, headers=OWNER)

    # Assert
    assert resp.status_code == 404


def test_given_course_owner_when_updating_module_then_changes_are_saved(db, sent):
    # Arrange
    course = _course(db)
    module = _module(db, course.id)
    resp = client.put(
        f"/{course.id}/modules/{module.id}",
        json={"title": "Renamed", "order": 4, "image_url": "https://img/m.png"},
        headers=OWNER,
    )

    # Act
    data = resp.json()

    # Assert
    assert (data["title"], data["order"], data["image_url"]) == (
        "Renamed",
        4,
        "https://img/m.png",
    )
    assert sent[-1][1]["position"] == 4


def test_given_module_with_children_when_deleted_then_its_children_are_removed(
    db, sent, tree
):
    # Arrange
    course, module, video = tree
    keep = _module(db, course.id)
    keep_video = _video(db, keep)
    _material(db, module)
    db.add(
        VideoCheckpoint(course_id=course.id, lesson_id=video.id, timestamp_seconds=1)
    )
    db.add(
        VideoCheckpoint(
            course_id=course.id, lesson_id=keep_video.id, timestamp_seconds=1
        )
    )
    db.commit()
    module_id = module.id

    # Act and assert
    assert client.delete(f"{_base(course, module)}", headers=OWNER).status_code == 204
    db.expire_all()

    # Act and assert
    assert db.get(Module, module_id) is None

    # Act and assert
    assert [lesson.id for lesson in db.query(Lesson)] == [keep_video.id]
    assert db.query(Material).count() == 0
    assert db.query(VideoCheckpoint).count() == 1
    assert sent[-1] == (ev.COURSE_MODULE_DELETED, {"id": module_id})


def test_given_empty_module_when_deleted_then_204(db):
    # Arrange
    course = _course(db)
    module = _module(db, course.id)

    # Act and assert
    assert client.delete(_base(course, module), headers=OWNER).status_code == 204


# ---------------------------------------------------------------------------
# Videos (lessons) and paid-content gating
# ---------------------------------------------------------------------------


def test_given_module_when_video_created_and_updated_then_changes_are_saved(db):
    # Arrange
    course = _course(db)
    module = _module(db, course.id)

    # Act
    resp = client.post(
        f"{_base(course, module)}/lessons",
        json={"title": "Intro", "youtube_video_id": "abc", "order": 1},
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 201
    lesson_id = resp.json()["id"]

    resp = client.put(
        f"{_base(course, module)}/lessons/{lesson_id}",
        json={"title": "Intro 2", "order": 5, "duration_seconds": 300},
        headers=OWNER,
    )

    # Act
    data = resp.json()

    # Assert
    assert (data["title"], data["order"], data["duration_seconds"]) == (
        "Intro 2",
        5,
        300,
    )


def test_given_video_without_youtube_id_when_created_then_422(db):
    # Arrange
    course = _course(db)
    module = _module(db, course.id)

    # Act
    resp = client.post(
        f"{_base(course, module)}/lessons", json={"title": "x"}, headers=OWNER
    )

    # Assert
    assert resp.status_code == 422


def test_given_unknown_video_when_updated_or_deleted_then_404_lesson_not_found(tree):
    # Arrange
    course, module, _ = tree
    base = _base(course, module)

    # Act and assert
    assert client.put(f"{base}/lessons/999", json={}, headers=OWNER).status_code == 404

    # Act
    resp = client.delete(f"{base}/lessons/999", headers=OWNER)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "LESSON_NOT_FOUND"


def test_given_video_with_pinned_materials_when_deleted_then_materials_are_unpinned(
    db, tree
):
    # Arrange
    course, module, video = tree
    pinned = _material(db, module, lesson_id=video.id)
    db.add(
        VideoCheckpoint(course_id=course.id, lesson_id=video.id, timestamp_seconds=1)
    )
    db.commit()

    # Act
    resp = client.delete(f"{_base(course, module)}/lessons/{video.id}", headers=OWNER)

    # Assert
    assert resp.status_code == 204
    db.expire_all()

    # Act and assert
    assert db.get(Material, pinned.id).lesson_id is None

    # Act and assert
    assert db.query(VideoCheckpoint).count() == 0


def test_given_no_token_when_listing_videos_then_401_auth_required(tree):
    # Arrange
    course, module, _ = tree

    # Act
    resp = client.get(f"{_base(course, module)}/lessons")

    # Assert
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "AUTH_REQUIRED"


def test_given_student_without_access_when_listing_videos_then_403_not_enrolled_and_with_access_then_200(
    db, tree
):
    # Arrange
    course, module, _ = tree
    url = f"{_base(course, module)}/lessons"

    # Act
    resp = client.get(url, headers=STUDENT)

    # Assert
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "NOT_ENROLLED"

    _grant(db, lessons=[module.id])  # a single lesson is enough

    # Act and assert
    assert client.get(url, headers=STUDENT).status_code == 200


def test_given_staff_without_enrollment_when_listing_videos_then_videos_are_returned(
    tree,
):
    # Arrange
    course, module, _ = tree

    # Act and assert
    assert (
        client.get(
            f"{_base(course, module)}/lessons", headers=OTHER_TEACHER
        ).status_code
        == 200
    )


def test_given_inactive_lesson_when_student_opens_it_then_404_while_staff_can_see_it(
    db,
):
    course = _course(db)
    module = _module(db, course.id, is_active=False)
    _grant(db, courses=[course.id])
    for path in ("lessons", "materials"):
        url = f"{_base(course, module)}/{path}"
        assert client.get(url, headers=STUDENT).status_code == 404
        assert client.get(url, headers=OWNER).status_code == 200


def test_given_videos_with_checkpoints_when_listed_then_checkpoints_are_in_order(
    db, tree
):
    # Arrange
    course, module, video = tree
    second = _video(db, module, title="Second", position=2)
    late = VideoCheckpoint(
        course_id=course.id, lesson_id=video.id, timestamp_seconds=90
    )
    early = VideoCheckpoint(
        course_id=course.id, lesson_id=video.id, timestamp_seconds=10
    )
    db.add_all([late, early])
    db.commit()
    for order, text in ((1, "Q2"), (0, "Q1")):
        db.add(
            CheckpointQuestion(
                checkpoint_id=early.id,
                question_text=text,
                option_a="a",
                option_b="b",
                correct_option="a",
                order_number=order,
            )
        )
    db.commit()
    _grant(db, courses=[course.id])

    # Act
    lessons = client.get(f"{_base(course, module)}/lessons", headers=STUDENT).json()

    # Assert
    assert [lesson["id"] for lesson in lessons] == [video.id, second.id]
    checkpoints = lessons[0]["checkpoints"]

    # Act and assert
    assert [c["timestamp_seconds"] for c in checkpoints] == [10, 90]
    assert [q["question_text"] for q in checkpoints[0]["questions"]] == ["Q1", "Q2"]
    assert checkpoints[1]["questions"] == []
    assert lessons[1]["checkpoints"] == []


def test_given_draft_course_when_student_lists_videos_then_404(db):
    # Arrange
    course = _course(db, status=CourseStatus.DRAFT)
    module = _module(db, course.id)
    _grant(db, courses=[course.id])

    # Act and assert
    assert (
        client.get(f"{_base(course, module)}/lessons", headers=STUDENT).status_code
        == 404
    )


def test_given_lesson_without_videos_when_listed_then_empty_list_returned(db):
    # Arrange
    course = _course(db)
    module = _module(db, course.id)

    # Act and assert
    assert client.get(f"{_base(course, module)}/lessons", headers=OWNER).json() == []


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------


def test_given_video_when_material_created_then_material_is_pinned_to_it(tree):
    # Arrange
    course, module, video = tree

    # Act
    resp = client.post(
        f"{_base(course, module)}/materials",
        json={
            "title": "Slides",
            "file_url": "https://files/s.pdf",
            "file_type": "pdf",
            "lesson_id": video.id,
            "order": 3,
        },
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 201
    assert (resp.json()["lesson_id"], resp.json()["order"]) == (video.id, 3)


def test_given_video_of_another_lesson_when_pinning_material_then_404_lesson_not_found(
    db, tree
):
    # Arrange
    course, module, _ = tree
    other_video = _video(db, _module(db, course.id))

    # Act
    resp = client.post(
        f"{_base(course, module)}/materials",
        json={
            "title": "x",
            "file_url": "u",
            "file_type": "pdf",
            "lesson_id": other_video.id,
        },
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "LESSON_NOT_FOUND"


def test_given_materials_when_listed_by_video_then_only_its_materials_returned_and_access_is_checked(
    db, tree
):
    # Arrange
    course, module, video = tree
    _material(db, module, title="General", position=2)
    _material(db, module, title="For video", lesson_id=video.id, position=1)

    url = f"{_base(course, module)}/materials"

    # Act and assert
    assert [m["title"] for m in client.get(url, headers=OWNER).json()] == [
        "For video",
        "General",
    ]

    # Act
    pinned = client.get(f"{url}?lesson_id={video.id}", headers=OWNER).json()

    # Assert
    assert [m["title"] for m in pinned] == ["For video"]

    # Act and assert
    assert client.get(url).status_code == 401
    assert client.get(url, headers=STUDENT).status_code == 403


def test_given_material_when_updated_then_fields_change_it_can_be_unpinned_and_unknown_lesson_is_404(
    db, tree
):
    # Arrange
    course, module, video = tree
    material = _material(db, module)
    url = f"{_base(course, module)}/materials/{material.id}"

    # Act
    resp = client.put(
        url, json={"title": "Renamed", "order": 2, "lesson_id": video.id}, headers=OWNER
    )

    # Assert
    assert (resp.json()["title"], resp.json()["order"], resp.json()["lesson_id"]) == (
        "Renamed",
        2,
        video.id,
    )
    # Unpinning is allowed.
    # Act and assert
    assert (
        client.put(url, json={"lesson_id": None}, headers=OWNER).json()["lesson_id"]
        is None
    )
    assert client.put(url, json={"lesson_id": 999}, headers=OWNER).status_code == 404


def test_given_missing_material_or_non_owner_when_changing_materials_then_404_or_403_and_owner_can_delete(
    db, tree
):
    # Arrange
    course, module, _ = tree
    material = _material(db, module)
    base = f"{_base(course, module)}/materials"

    # Act
    resp = client.put(f"{base}/999", json={}, headers=OWNER)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "MATERIAL_NOT_FOUND"

    # Act and assert
    assert (
        client.delete(f"{base}/{material.id}", headers=OTHER_TEACHER).status_code == 403
    )
    assert client.delete(f"{base}/{material.id}", headers=OWNER).status_code == 204
    assert client.delete(f"{base}/{material.id}", headers=OWNER).status_code == 404


# ---------------------------------------------------------------------------
# In-video checkpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, seconds",
    [("90", 90), ("1:30", 90), ("01:02:03", 3723), (" 0:05 ", 5)],
)
def test_given_timestamp_strings_when_parsed_then_seconds_are_returned(raw, seconds):
    # Act and assert
    assert _parse_timestamp(raw) == seconds


@pytest.mark.parametrize("raw", ["", "1:2:3:4", "a:10", "1.5", None])
def test_given_invalid_timestamp_strings_when_parsed_then_they_are_rejected(raw):
    # Act and assert
    assert _parse_timestamp(raw) is None


def test_given_staff_or_student_when_checkpoint_template_requested_then_staff_gets_csv_and_student_is_403():
    # Act
    resp = client.get("/checkpoints/csv-template", headers=OWNER)

    # Assert
    assert resp.status_code == 200
    assert resp.text.startswith("timestamp_seconds,question_text,a,b,c,d,e,")

    # Act and assert
    assert client.get("/checkpoints/csv-template", headers=STUDENT).status_code == 403


def _import(course, module, video, text: bytes, headers=OWNER):
    return client.post(
        f"{_base(course, module)}/lessons/{video.id}/checkpoints/import-csv",
        files={"file": ("cp.csv", text, "text/csv")},
        headers=headers,
    )


CHECKPOINT_CSV = (
    "timestamp_seconds,question_text,a,b,c,d,e,correct_option,explanation\n"
    "1:30,First?,Yes,No,,,,A,Because\n"
    "90,Same time?,X,Y,Z,,,c,\n"
    "10,Early?,P,Q,,,,b,\n"
    "soon,Bad time,a,b,,,,a,\n"
    "20,,a,b,,,,a,\n"
    "30,Bad answer,a,b,,,,d,\n"
)


def test_given_csv_when_checkpoints_imported_then_checkpoints_are_created(db, tree):
    # Arrange
    course, module, video = tree

    # Act
    resp = _import(course, module, video, ("﻿" + CHECKPOINT_CSV).encode())

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()

    # Assert
    assert data["questions_created"] == 3
    assert data["message"] == "Imported 3 question(s) across 2 checkpoint(s)"
    assert data["errors"] == [
        "Row 5: timestamp must be seconds or MM:SS",
        "Row 6: question_text, a and b are required",
        "Row 7: correct_option must be one of a, b",
    ]

    # Act
    listed = client.get(
        f"{_base(course, module)}/lessons/{video.id}/checkpoints", headers=OWNER
    ).json()

    # Assert
    assert [c["timestamp_seconds"] for c in listed] == [10, 90]
    at_90 = listed[1]["questions"]

    # Act and assert
    assert [(q["question_text"], q["order_number"]) for q in at_90] == [
        ("First?", 0),
        ("Same time?", 1),
    ]
    assert at_90[0]["correct_option"] == "a" and at_90[0]["explanation"] == "Because"
    assert at_90[1]["option_c"] == "Z" and at_90[1]["option_d"] is None


def test_given_existing_checkpoint_when_csv_imported_then_questions_are_added_to_it(
    db, tree
):
    # Arrange
    course, module, video = tree
    _import(course, module, video, CHECKPOINT_CSV.encode())

    _import(
        course,
        module,
        video,
        b"timestamp_seconds,question_text,a,b,correct_option\n90,Third?,a,b,a\n",
    )

    # Act and assert
    assert db.query(VideoCheckpoint).count() == 2
    assert db.query(CheckpointQuestion).count() == 4


def test_given_bad_csv_encoding_permissions_or_size_when_importing_checkpoints_then_each_error_is_returned(
    tree, monkeypatch
):
    # Arrange
    course, module, video = tree
    import router

    bad = _import(course, module, video, "timestamp_seconds\nCafé".encode("latin-1"))

    # Act and assert
    assert bad.status_code == 422
    assert bad.json()["detail"]["error"] == "INVALID_ENCODING"
    assert _import(course, module, video, b"", headers=OTHER_TEACHER).status_code == 403
    monkeypatch.setattr(router, "_MAX_CHECKPOINT_CSV", 5)

    # Act and assert
    assert _import(course, module, video, b"0123456789").status_code == 413


def test_given_missing_video_when_checkpoints_listed_then_404(tree):
    # Arrange
    course, module, _ = tree

    # Act
    resp = client.get(f"{_base(course, module)}/lessons/999/checkpoints", headers=OWNER)

    # Assert
    assert resp.status_code == 404


def test_given_video_with_checkpoints_when_cleared_then_204_and_checkpoints_are_removed(
    db, tree
):
    # Arrange
    course, module, video = tree
    _import(course, module, video, CHECKPOINT_CSV.encode())

    # Act
    resp = client.delete(
        f"{_base(course, module)}/lessons/{video.id}/checkpoints", headers=OWNER
    )

    # Assert
    assert resp.status_code == 204
    assert db.query(VideoCheckpoint).count() == 0
    assert db.query(CheckpointQuestion).count() == 0


@pytest.fixture
def checkpoint(db, tree):
    course, module, video = tree
    cp = VideoCheckpoint(course_id=course.id, lesson_id=video.id, timestamp_seconds=30)
    db.add(cp)
    db.commit()
    q = CheckpointQuestion(
        checkpoint_id=cp.id,
        question_text="Q",
        option_a="A",
        option_b="B",
        correct_option="a",
    )
    db.add(q)
    db.commit()
    return cp, q


def test_given_checkpoint_when_moved_then_timestamp_changes_and_invalid_timestamp_is_rejected(
    checkpoint,
):
    # Arrange
    cp, _ = checkpoint

    # Act
    resp = client.patch(
        f"/checkpoints/{cp.id}", json={"timestamp_seconds": 45}, headers=OWNER
    )

    # Assert
    assert resp.status_code == 200
    assert resp.json()["timestamp_seconds"] == 45 and len(resp.json()["questions"]) == 1

    # Act
    bad = client.patch(
        f"/checkpoints/{cp.id}", json={"timestamp_seconds": -1}, headers=OWNER
    )

    # Assert
    assert bad.json()["detail"]["error"] == "INVALID_TIMESTAMP"


def test_given_non_owner_or_missing_checkpoint_when_changed_then_403_or_404(checkpoint):
    # Arrange
    cp, _ = checkpoint
    body = {"timestamp_seconds": 1}

    # Act and assert
    assert (
        client.patch(
            f"/checkpoints/{cp.id}", json=body, headers=OTHER_TEACHER
        ).status_code
        == 403
    )

    # Act
    resp = client.patch("/checkpoints/999", json=body, headers=OWNER)

    # Assert
    assert resp.json()["detail"]["error"] == "CHECKPOINT_NOT_FOUND"

    # Act and assert
    assert client.delete("/checkpoints/999", headers=OWNER).status_code == 404


def test_given_checkpoint_when_deleted_then_204(db, checkpoint):
    # Arrange
    cp, _ = checkpoint

    # Act and assert
    assert client.delete(f"/checkpoints/{cp.id}", headers=OWNER).status_code == 204

    # Act and assert
    assert db.query(CheckpointQuestion).count() == 0


def test_given_checkpoint_question_when_edited_then_changes_are_saved(checkpoint):
    # Arrange
    _, q = checkpoint

    # Act
    resp = client.patch(
        f"/checkpoint-questions/{q.id}",
        json={"question_text": "  New?  ", "option_c": " C ", "correct_option": "C"},
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()

    # Assert
    assert (data["question_text"], data["option_c"], data["correct_option"]) == (
        "New?",
        "C",
        "c",
    )


def test_given_invalid_answer_or_unknown_question_when_editing_then_422_or_404(
    db, checkpoint
):
    # Arrange
    _, q = checkpoint
    url = f"/checkpoint-questions/{q.id}"

    # Act
    blank = client.patch(url, json={"option_a": "  "}, headers=OWNER)

    # Assert
    assert blank.status_code == 422
    assert blank.json()["detail"]["message"] == "option_a cannot be empty"

    # Act
    empty_answer = client.patch(url, json={"correct_option": "e"}, headers=OWNER)

    # Assert
    assert empty_answer.json()["detail"]["error"] == "INVALID_ANSWER"
    db.refresh(q)

    # Act and assert
    assert q.correct_option == "a"  # rolled back

    # Act and assert
    assert (
        client.patch(url, json={"correct_option": "z"}, headers=OWNER).status_code
        == 422
    )
    assert client.patch(url, json={}, headers=OTHER_TEACHER).status_code == 403

    # Act
    missing = client.patch("/checkpoint-questions/999", json={}, headers=OWNER)

    # Assert
    assert missing.json()["detail"]["error"] == "QUESTION_NOT_FOUND"


# ---------------------------------------------------------------------------
# Event payloads
# ---------------------------------------------------------------------------


def test_given_course_without_optional_fields_when_event_built_then_defaults_are_used(
    db,
):
    # Arrange
    course = _course(db)

    payload = events.course_event(course)

    # Act and assert
    assert payload["stream_ids"] == [] and payload["price"] == 0.0
    assert payload["course_type"] == "video"
    assert json.dumps(payload)  # serialisable for the broker
