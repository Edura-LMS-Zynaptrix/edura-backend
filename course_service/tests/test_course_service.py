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


def test_health():
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


def test_startup_creates_tables_and_starts_listeners(startup):
    startup_engine, calls = startup
    main.on_startup()  # SQLite rejects ADD COLUMN IF NOT EXISTS; logged, not fatal
    assert "courses" in startup_engine.dialect.get_table_names(startup_engine.connect())
    assert calls == ["consumer", ("resync", main._resync)]


def test_startup_survives_create_all_failure(startup, monkeypatch):
    _, calls = startup

    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    main.on_startup()
    assert calls[0] == "consumer"


def test_resync_reannounces_every_course_and_lesson(db, sent, monkeypatch):
    course = _course(db, stream_ids=[2], price=Decimal("1500"))
    module = _module(db, course.id, position=3)
    monkeypatch.setattr(main, "SessionLocal", TestingSessionLocal)

    main._resync()

    assert sent == [
        (ev.COURSE_UPSERTED, events.course_event(course)),
        (ev.COURSE_MODULE_UPSERTED, events.module_event(module)),
    ]
    assert sent[0][1]["price"] == 1500.0 and sent[0][1]["status"] == "PUBLISHED"


def test_consumer_stores_access_and_start_binds_queue(db, monkeypatch):
    event_consumer.handle(
        "access.changed",
        {"student_id": 101, "courses": [{"id": 1, "until": None}], "lessons": [9]},
    )
    assert asyncio.run(access.is_enrolled(101, 1)) is True
    assert asyncio.run(access.is_enrolled(101, 2, 9)) is True
    assert asyncio.run(access.is_enrolled(101, 2, 8)) is False

    calls = []
    monkeypatch.setattr(
        event_consumer.messaging, "start_consumer", lambda *args: calls.append(args)
    )
    event_consumer.start()
    assert calls == [
        ("course", "course.queue", ev.QUEUES["course.queue"], event_consumer.handle)
    ]


def test_consumer_rolls_back_and_reraises(monkeypatch):
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
    with pytest.raises(ValueError):
        event_consumer.handle("access.changed", {})
    assert fake.rolled_back and fake.closed


# ---------------------------------------------------------------------------
# Browsing the catalogue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grade, allowed", [(11, [10, 11]), (13, [12, 13]), (10, [10]), (12, [12])]
)
def test_grade_bands(grade, allowed):
    assert get_allowed_grades_for_student(grade) == allowed


def test_public_sees_only_published_courses(db):
    _course(db, title="Live")
    _course(db, title="Draft", status=CourseStatus.DRAFT)
    _course(db, title="Old", status=CourseStatus.ARCHIVED)

    for headers in ({}, STUDENT):
        data = client.get("/", headers=headers).json()
        assert [c["title"] for c in data["items"]] == ["Live"]
        assert data["total"] == 1


def test_staff_filter_by_status(db):
    _course(db, title="Live")
    _course(db, title="Draft", status=CourseStatus.DRAFT)

    assert client.get("/", headers=ADMIN).json()["total"] == 2
    drafts = client.get("/?status=DRAFT", headers=OWNER).json()["items"]
    assert [c["title"] for c in drafts] == ["Draft"]
    resp = client.get("/?status=HIDDEN", headers=ADMIN)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "INVALID_STATUS"


def test_grade_filter_keeps_ungraded_courses(db):
    for g in (10, 11, 12, None):
        _course(db, title=f"G{g}", grade=g)
    titles = [c["title"] for c in client.get("/?grade=11").json()["items"]]
    assert titles == ["G10", "G11", "GNone"]


def test_stream_filter_applies_only_at_a_level(db):
    _course(db, title="Physics", grade=12, stream_ids=[1, 2])
    _course(db, title="Biology", grade=12, stream_ids=[2])
    _course(db, title="Maths", grade=12, stream_ids=[1])
    _course(db, title="General", grade=12, stream_ids=None)

    resp = client.get("/?grade=12&stream_id=2").json()
    assert [c["title"] for c in resp["items"]] == ["Physics", "Biology", "General"]
    assert resp["total"] == 3
    page = client.get("/?grade=12&stream_id=2&page=2&page_size=2").json()
    assert [c["title"] for c in page["items"]] == ["General"] and page["total"] == 3
    # Below A/L the stream is ignored.
    _course(db, title="O/L", grade=10, stream_ids=[9])
    assert client.get("/?grade=10&stream_id=2").json()["total"] == 1


def test_pagination_and_page_size_limits(db):
    for i in range(3):
        _course(db, title=f"C{i}")
    page = client.get("/?page=2&page_size=2").json()
    assert (page["total"], page["page"], [c["title"] for c in page["items"]]) == (
        3,
        2,
        ["C2"],
    )
    assert client.get("/?page=0").status_code == 422
    assert client.get("/?page_size=101").status_code == 422


def test_include_modules_hides_inactive_lessons_from_students(db):
    course = _course(db)
    active = _module(db, course.id, title="Active", position=1)
    _module(db, course.id, title="Hidden", position=2, is_active=False)
    _video(db, active)
    _video(db, active)

    item = client.get("/?include_modules=true").json()["items"][0]
    assert [(m["title"], m["lesson_count"]) for m in item["modules"]] == [("Active", 2)]
    staff = client.get("/?include_modules=true", headers=OWNER).json()["items"][0]
    assert [m["title"] for m in staff["modules"]] == ["Active", "Hidden"]
    # Without the flag, modules are null (not "no modules").
    assert client.get("/").json()["items"][0]["modules"] is None


def test_include_modules_with_no_courses():
    assert client.get("/?include_modules=true").json()["items"] == []


def test_course_without_modules_lists_empty(db):
    course = _course(db)
    assert client.get(f"/{course.id}?include_modules=true").json()["modules"] == []


def test_get_course_visibility(db):
    draft = _course(db, status=CourseStatus.DRAFT)
    assert client.get(f"/{draft.id}").status_code == 404
    assert client.get(f"/{draft.id}", headers=STUDENT).status_code == 404
    assert client.get(f"/{draft.id}", headers=OWNER).status_code == 200
    resp = client.get("/999")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "COURSE_NOT_FOUND"


def test_list_modules(db):
    course = _course(db)
    _module(db, course.id, title="B", position=2)
    _module(db, course.id, title="A", position=1)
    assert [m["title"] for m in client.get(f"/{course.id}/modules").json()] == [
        "A",
        "B",
    ]
    draft = _course(db, status=CourseStatus.DRAFT)
    assert client.get(f"/{draft.id}/modules").status_code == 404


# ---------------------------------------------------------------------------
# Course authoring
# ---------------------------------------------------------------------------


def test_create_course_as_draft(sent):
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
    assert resp.status_code == 201
    data = resp.json()
    assert (data["status"], data["teacher_id"], data["course_type"]) == (
        "DRAFT",
        5,
        "exam",
    )
    assert data["stream_ids"] == [1, 3]
    key, payload = sent[-1]
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
def test_create_course_validation(body):
    assert client.post("/", json=body, headers=OWNER).status_code == 422


def test_create_course_permissions():
    assert client.post("/", json={"title": "x"}).status_code == 401
    assert client.post("/", json={"title": "x"}, headers=STUDENT).status_code == 403


def test_update_course_fields(db, sent):
    course = _course(db, status=CourseStatus.DRAFT)
    resp = client.put(
        f"/{course.id}",
        json={"title": "Renamed", "price": "99.50", "course_type": "exam"},
        headers=OWNER,
    )
    assert resp.status_code == 200
    data = resp.json()
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
def test_update_course_validation(db, body):
    course = _course(db)
    assert client.put(f"/{course.id}", json=body, headers=OWNER).status_code == 422


def test_publishing_a_video_course_needs_a_video(db, sent):
    course = _course(db, status=CourseStatus.DRAFT)
    module = _module(db, course.id)

    resp = client.put(f"/{course.id}", json={"status": "PUBLISHED"}, headers=OWNER)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "PUBLISH_REQUIRES_CONTENT"
    assert "video" in resp.json()["detail"]["message"]

    _video(db, module)
    resp = client.put(f"/{course.id}", json={"status": "PUBLISHED"}, headers=OWNER)
    assert resp.status_code == 200 and resp.json()["status"] == "PUBLISHED"
    db.refresh(course)
    assert course.is_published is True
    assert sent[-1] == (
        ev.COURSE_PUBLISHED,
        {"course_id": course.id, "teacher_id": 5, "title": "Python"},
    )


def test_publishing_an_exam_course_needs_only_a_lesson(db):
    course = _course(db, status=CourseStatus.DRAFT, course_type=CourseType.exam)
    resp = client.put(f"/{course.id}", json={"status": "PUBLISHED"}, headers=OWNER)
    assert resp.status_code == 422
    assert (
        resp.json()["detail"]["message"] == "Add at least one lesson before publishing"
    )

    _module(db, course.id)
    assert (
        client.put(
            f"/{course.id}", json={"status": "PUBLISHED"}, headers=OWNER
        ).status_code
        == 200
    )


def test_switching_to_exam_while_publishing_uses_the_new_type(db):
    course = _course(db, status=CourseStatus.DRAFT)
    _module(db, course.id)  # a lesson, but no video
    resp = client.put(
        f"/{course.id}",
        json={"status": "PUBLISHED", "course_type": "exam"},
        headers=OWNER,
    )
    assert resp.status_code == 200


def test_archiving_unpublishes(db, sent):
    course = _course(db, is_published=True)
    resp = client.put(f"/{course.id}", json={"status": "ARCHIVED"}, headers=OWNER)
    assert resp.json()["status"] == "ARCHIVED"
    db.refresh(course)
    assert course.is_published is False
    assert ev.COURSE_PUBLISHED not in [k for k, _ in sent]


def test_course_ownership(db):
    course = _course(db)
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


def test_delete_course_removes_everything_under_it(db, sent, tree):
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

    assert client.delete(f"/{course_id}", headers=OWNER).status_code == 204

    db.expire_all()
    assert db.get(Course, course_id) is None
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


def test_create_module(db, sent):
    course = _course(db)
    resp = client.post(
        f"/{course.id}/modules",
        json={"title": "Algebra", "order": 2, "is_active": False},
        headers=OWNER,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert (data["order"], data["is_active"], data["lesson_count"]) == (2, False, 0)
    assert sent[-1][0] == ev.COURSE_MODULE_UPSERTED
    assert sent[-1][1]["is_active"] is False


def test_module_ownership_and_missing(db):
    course = _course(db)
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
    resp = client.put(f"/{course.id}/modules/999", json={}, headers=OWNER)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "MODULE_NOT_FOUND"


def test_module_must_belong_to_the_course(db):
    a = _course(db)
    b = _course(db)
    module_of_b = _module(db, b.id)
    resp = client.put(f"/{a.id}/modules/{module_of_b.id}", json={}, headers=OWNER)
    assert resp.status_code == 404


def test_update_module(db, sent):
    course = _course(db)
    module = _module(db, course.id)
    resp = client.put(
        f"/{course.id}/modules/{module.id}",
        json={"title": "Renamed", "order": 4, "image_url": "https://img/m.png"},
        headers=OWNER,
    )
    data = resp.json()
    assert (data["title"], data["order"], data["image_url"]) == (
        "Renamed",
        4,
        "https://img/m.png",
    )
    assert sent[-1][1]["position"] == 4


def test_delete_module_clears_its_children(db, sent, tree):
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

    assert client.delete(f"{_base(course, module)}", headers=OWNER).status_code == 204
    db.expire_all()
    assert db.get(Module, module_id) is None
    assert [lesson.id for lesson in db.query(Lesson)] == [keep_video.id]
    assert db.query(Material).count() == 0
    assert db.query(VideoCheckpoint).count() == 1
    assert sent[-1] == (ev.COURSE_MODULE_DELETED, {"id": module_id})


def test_delete_empty_module(db):
    course = _course(db)
    module = _module(db, course.id)
    assert client.delete(_base(course, module), headers=OWNER).status_code == 204


# ---------------------------------------------------------------------------
# Videos (lessons) and paid-content gating
# ---------------------------------------------------------------------------


def test_create_and_update_video(db):
    course = _course(db)
    module = _module(db, course.id)
    resp = client.post(
        f"{_base(course, module)}/lessons",
        json={"title": "Intro", "youtube_video_id": "abc", "order": 1},
        headers=OWNER,
    )
    assert resp.status_code == 201
    lesson_id = resp.json()["id"]

    resp = client.put(
        f"{_base(course, module)}/lessons/{lesson_id}",
        json={"title": "Intro 2", "order": 5, "duration_seconds": 300},
        headers=OWNER,
    )
    data = resp.json()
    assert (data["title"], data["order"], data["duration_seconds"]) == (
        "Intro 2",
        5,
        300,
    )


def test_create_video_needs_a_youtube_id(db):
    course = _course(db)
    module = _module(db, course.id)
    resp = client.post(
        f"{_base(course, module)}/lessons", json={"title": "x"}, headers=OWNER
    )
    assert resp.status_code == 422


def test_video_not_found(tree):
    course, module, _ = tree
    base = _base(course, module)
    assert client.put(f"{base}/lessons/999", json={}, headers=OWNER).status_code == 404
    resp = client.delete(f"{base}/lessons/999", headers=OWNER)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "LESSON_NOT_FOUND"


def test_delete_video_unpins_its_materials(db, tree):
    course, module, video = tree
    pinned = _material(db, module, lesson_id=video.id)
    db.add(
        VideoCheckpoint(course_id=course.id, lesson_id=video.id, timestamp_seconds=1)
    )
    db.commit()

    resp = client.delete(f"{_base(course, module)}/lessons/{video.id}", headers=OWNER)
    assert resp.status_code == 204
    db.expire_all()
    assert db.get(Material, pinned.id).lesson_id is None
    assert db.query(VideoCheckpoint).count() == 0


def test_videos_need_sign_in(tree):
    course, module, _ = tree
    resp = client.get(f"{_base(course, module)}/lessons")
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "AUTH_REQUIRED"


def test_students_need_access_to_open_videos(db, tree):
    course, module, _ = tree
    url = f"{_base(course, module)}/lessons"
    resp = client.get(url, headers=STUDENT)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "NOT_ENROLLED"

    _grant(db, lessons=[module.id])  # a single lesson is enough
    assert client.get(url, headers=STUDENT).status_code == 200


def test_staff_open_videos_without_enrollment(tree):
    course, module, _ = tree
    assert (
        client.get(
            f"{_base(course, module)}/lessons", headers=OTHER_TEACHER
        ).status_code
        == 200
    )


def test_inactive_lesson_is_hidden_from_students(db):
    course = _course(db)
    module = _module(db, course.id, is_active=False)
    _grant(db, courses=[course.id])
    for path in ("lessons", "materials"):
        url = f"{_base(course, module)}/{path}"
        assert client.get(url, headers=STUDENT).status_code == 404
        assert client.get(url, headers=OWNER).status_code == 200


def test_videos_list_with_checkpoints_in_order(db, tree):
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

    lessons = client.get(f"{_base(course, module)}/lessons", headers=STUDENT).json()
    assert [lesson["id"] for lesson in lessons] == [video.id, second.id]
    checkpoints = lessons[0]["checkpoints"]
    assert [c["timestamp_seconds"] for c in checkpoints] == [10, 90]
    assert [q["question_text"] for q in checkpoints[0]["questions"]] == ["Q1", "Q2"]
    assert checkpoints[1]["questions"] == []
    assert lessons[1]["checkpoints"] == []


def test_videos_of_a_draft_course_are_hidden(db):
    course = _course(db, status=CourseStatus.DRAFT)
    module = _module(db, course.id)
    _grant(db, courses=[course.id])
    assert (
        client.get(f"{_base(course, module)}/lessons", headers=STUDENT).status_code
        == 404
    )


def test_empty_lesson_has_no_videos(db):
    course = _course(db)
    module = _module(db, course.id)
    assert client.get(f"{_base(course, module)}/lessons", headers=OWNER).json() == []


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------


def test_create_material_pinned_to_a_video(tree):
    course, module, video = tree
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
    assert resp.status_code == 201
    assert (resp.json()["lesson_id"], resp.json()["order"]) == (video.id, 3)


def test_material_cannot_pin_a_video_from_another_lesson(db, tree):
    course, module, _ = tree
    other_video = _video(db, _module(db, course.id))
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
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "LESSON_NOT_FOUND"


def test_list_materials_filtered_by_video(db, tree):
    course, module, video = tree
    _material(db, module, title="General", position=2)
    _material(db, module, title="For video", lesson_id=video.id, position=1)

    url = f"{_base(course, module)}/materials"
    assert [m["title"] for m in client.get(url, headers=OWNER).json()] == [
        "For video",
        "General",
    ]
    pinned = client.get(f"{url}?lesson_id={video.id}", headers=OWNER).json()
    assert [m["title"] for m in pinned] == ["For video"]
    assert client.get(url).status_code == 401
    assert client.get(url, headers=STUDENT).status_code == 403


def test_update_material(db, tree):
    course, module, video = tree
    material = _material(db, module)
    url = f"{_base(course, module)}/materials/{material.id}"

    resp = client.put(
        url, json={"title": "Renamed", "order": 2, "lesson_id": video.id}, headers=OWNER
    )
    assert (resp.json()["title"], resp.json()["order"], resp.json()["lesson_id"]) == (
        "Renamed",
        2,
        video.id,
    )
    # Unpinning is allowed.
    assert (
        client.put(url, json={"lesson_id": None}, headers=OWNER).json()["lesson_id"]
        is None
    )
    assert client.put(url, json={"lesson_id": 999}, headers=OWNER).status_code == 404


def test_material_not_found_and_delete(db, tree):
    course, module, _ = tree
    material = _material(db, module)
    base = f"{_base(course, module)}/materials"
    resp = client.put(f"{base}/999", json={}, headers=OWNER)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "MATERIAL_NOT_FOUND"
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
def test_parse_timestamp(raw, seconds):
    assert _parse_timestamp(raw) == seconds


@pytest.mark.parametrize("raw", ["", "1:2:3:4", "a:10", "1.5", None])
def test_parse_timestamp_rejects(raw):
    assert _parse_timestamp(raw) is None


def test_checkpoint_template():
    resp = client.get("/checkpoints/csv-template", headers=OWNER)
    assert resp.status_code == 200
    assert resp.text.startswith("timestamp_seconds,question_text,a,b,c,d,e,")
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


def test_import_checkpoints(db, tree):
    course, module, video = tree
    resp = _import(course, module, video, ("﻿" + CHECKPOINT_CSV).encode())
    assert resp.status_code == 200
    data = resp.json()
    assert data["questions_created"] == 3
    assert data["message"] == "Imported 3 question(s) across 2 checkpoint(s)"
    assert data["errors"] == [
        "Row 5: timestamp must be seconds or MM:SS",
        "Row 6: question_text, a and b are required",
        "Row 7: correct_option must be one of a, b",
    ]

    listed = client.get(
        f"{_base(course, module)}/lessons/{video.id}/checkpoints", headers=OWNER
    ).json()
    assert [c["timestamp_seconds"] for c in listed] == [10, 90]
    at_90 = listed[1]["questions"]
    assert [(q["question_text"], q["order_number"]) for q in at_90] == [
        ("First?", 0),
        ("Same time?", 1),
    ]
    assert at_90[0]["correct_option"] == "a" and at_90[0]["explanation"] == "Because"
    assert at_90[1]["option_c"] == "Z" and at_90[1]["option_d"] is None


def test_import_adds_to_an_existing_checkpoint(db, tree):
    course, module, video = tree
    _import(course, module, video, CHECKPOINT_CSV.encode())
    _import(
        course,
        module,
        video,
        b"timestamp_seconds,question_text,a,b,correct_option\n90,Third?,a,b,a\n",
    )
    assert db.query(VideoCheckpoint).count() == 2
    assert db.query(CheckpointQuestion).count() == 4


def test_import_checkpoint_errors(tree, monkeypatch):
    course, module, video = tree
    import router

    bad = _import(course, module, video, "timestamp_seconds\nCafé".encode("latin-1"))
    assert bad.status_code == 422
    assert bad.json()["detail"]["error"] == "INVALID_ENCODING"
    assert _import(course, module, video, b"", headers=OTHER_TEACHER).status_code == 403
    monkeypatch.setattr(router, "_MAX_CHECKPOINT_CSV", 5)
    assert _import(course, module, video, b"0123456789").status_code == 413


def test_checkpoints_for_a_missing_video(tree):
    course, module, _ = tree
    resp = client.get(f"{_base(course, module)}/lessons/999/checkpoints", headers=OWNER)
    assert resp.status_code == 404


def test_clear_checkpoints(db, tree):
    course, module, video = tree
    _import(course, module, video, CHECKPOINT_CSV.encode())
    resp = client.delete(
        f"{_base(course, module)}/lessons/{video.id}/checkpoints", headers=OWNER
    )
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


def test_move_checkpoint(checkpoint):
    cp, _ = checkpoint
    resp = client.patch(
        f"/checkpoints/{cp.id}", json={"timestamp_seconds": 45}, headers=OWNER
    )
    assert resp.status_code == 200
    assert resp.json()["timestamp_seconds"] == 45 and len(resp.json()["questions"]) == 1
    bad = client.patch(
        f"/checkpoints/{cp.id}", json={"timestamp_seconds": -1}, headers=OWNER
    )
    assert bad.json()["detail"]["error"] == "INVALID_TIMESTAMP"


def test_checkpoint_ownership_and_missing(checkpoint):
    cp, _ = checkpoint
    body = {"timestamp_seconds": 1}
    assert (
        client.patch(
            f"/checkpoints/{cp.id}", json=body, headers=OTHER_TEACHER
        ).status_code
        == 403
    )
    resp = client.patch("/checkpoints/999", json=body, headers=OWNER)
    assert resp.json()["detail"]["error"] == "CHECKPOINT_NOT_FOUND"
    assert client.delete("/checkpoints/999", headers=OWNER).status_code == 404


def test_delete_checkpoint(db, checkpoint):
    cp, _ = checkpoint
    assert client.delete(f"/checkpoints/{cp.id}", headers=OWNER).status_code == 204
    assert db.query(CheckpointQuestion).count() == 0


def test_edit_checkpoint_question(checkpoint):
    _, q = checkpoint
    resp = client.patch(
        f"/checkpoint-questions/{q.id}",
        json={"question_text": "  New?  ", "option_c": " C ", "correct_option": "C"},
        headers=OWNER,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert (data["question_text"], data["option_c"], data["correct_option"]) == (
        "New?",
        "C",
        "c",
    )


def test_checkpoint_question_validation(db, checkpoint):
    _, q = checkpoint
    url = f"/checkpoint-questions/{q.id}"

    blank = client.patch(url, json={"option_a": "  "}, headers=OWNER)
    assert blank.status_code == 422
    assert blank.json()["detail"]["message"] == "option_a cannot be empty"

    empty_answer = client.patch(url, json={"correct_option": "e"}, headers=OWNER)
    assert empty_answer.json()["detail"]["error"] == "INVALID_ANSWER"
    db.refresh(q)
    assert q.correct_option == "a"  # rolled back

    assert (
        client.patch(url, json={"correct_option": "z"}, headers=OWNER).status_code
        == 422
    )
    assert client.patch(url, json={}, headers=OTHER_TEACHER).status_code == 403
    missing = client.patch("/checkpoint-questions/999", json={}, headers=OWNER)
    assert missing.json()["detail"]["error"] == "QUESTION_NOT_FOUND"


# ---------------------------------------------------------------------------
# Event payloads
# ---------------------------------------------------------------------------


def test_course_event_defaults(db):
    course = _course(db)
    payload = events.course_event(course)
    assert payload["stream_ids"] == [] and payload["price"] == 0.0
    assert payload["course_type"] == "video"
    assert json.dumps(payload)  # serialisable for the broker
