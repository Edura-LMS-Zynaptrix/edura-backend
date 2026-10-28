"""Gaps not covered by test_assessment.py: app startup, exam and question authoring,
the images ZIP and bulk import, student routes' edge cases, rankings, every badge
rule, the resync, and the helper modules."""

import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import badges
import bulk_import
import catalog
import event_consumer
import events
import images
import jwt
import main
import people
import pytest
import redis_client
import resync
import router
from database import Base, get_db
from fastapi.testclient import TestClient
from main import app
from models import (
    Assessment,
    AssessmentType,
    Badge,
    Question,
    QuestionType,
    ReplicaCourse,
    ReplicaUser,
    Submission,
    SubmissionStatus,
    UserBadge,
    ViolationLog,
    ViolationType,
)
from redis_client import MemoryRedisClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import events as ev
from shared import media
from shared.auth import ALGORITHM, SECRET_KEY

# A separate SQLite database from test_assessment.py; installed per test (see
# `isolated`) so neither file's dependency override leaks into the other.
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
        {"sub": str(user_id), "role": role}, SECRET_KEY, algorithm=ALGORITHM
    )
    return {"Authorization": f"Bearer {token}"}


STUDENT = _auth(101, "student")
OTHER_STUDENT = _auth(102, "student")
OWNER = _auth(5, "teacher")  # instructor of course 10
OTHER_TEACHER = _auth(6, "teacher")
ADMIN = _auth(1, "admin")

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setitem(app.dependency_overrides, get_db, override_get_db)
    for module in (catalog, people, event_consumer, resync):
        monkeypatch.setattr(module, "SessionLocal", TestingSessionLocal)

    async def enrolled(*args):
        return True

    monkeypatch.setattr(router, "is_enrolled", enrolled)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def redis(monkeypatch):
    store = MemoryRedisClient()
    monkeypatch.setattr(router, "get_redis_client", lambda: store)
    return store


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Every event the service publishes, as (routing_key, payload)."""
    published = []
    monkeypatch.setattr(
        events, "publish", lambda key, payload: published.append((key, payload)) or True
    )
    return published


@pytest.fixture(autouse=True)
def uploads(monkeypatch):
    """Cloudinary stand-in: names containing "bad" fail, the rest get a fake URL."""
    stored = []

    async def upload_image(data, filename):
        if "bad" in filename:
            raise ValueError("Image content is 'unknown', which is not allowed")
        stored.append(filename)
        return f"https://img/{filename}", f"pid-{filename}"

    monkeypatch.setattr(bulk_import, "upload_image", upload_image)
    return stored


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seeded_badges(db):
    badges.seed_badges(db)


def _course(db, course_id=10, instructor_id=5, title="Python"):
    db.add(
        ReplicaCourse(
            id=course_id, title=title, instructor_id=instructor_id, status="PUBLISHED"
        )
    )
    db.commit()


def _student(db, user_id=101, **fields):
    fields.setdefault("full_name", f"Student {user_id}")
    db.add(ReplicaUser(user_id=user_id, role="student", **fields))
    db.commit()


def _exam(db, **fields) -> Assessment:
    fields.setdefault("course_id", 10)
    fields.setdefault("title", "Quiz")
    fields.setdefault("assessment_type", AssessmentType.quiz)
    fields.setdefault("time_limit_minutes", 15)
    fields.setdefault("pass_score", 50)
    fields.setdefault("is_published", True)
    exam = Assessment(**fields)
    db.add(exam)
    db.commit()
    db.refresh(exam)
    return exam


def _question(db, exam_id, text="Q", options=("A", "B"), answer="A", **fields):
    q = Question(
        assessment_id=exam_id,
        question_text=text,
        question_type=fields.pop("question_type", QuestionType.mcq),
        options_json=json.dumps(list(options)) if options is not None else None,
        correct_answer=answer,
        marks=fields.pop("marks", 1),
        position=fields.pop("position", 1),
        **fields,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def _graded(db, exam_id, student_id=101, score=80.0, when=None, seconds=120, **f):
    when = when or datetime.now(timezone.utc)
    sub = Submission(
        assessment_id=exam_id,
        student_id=student_id,
        status=SubmissionStatus.graded,
        score=score,
        started_at=when - timedelta(seconds=seconds),
        submitted_at=when,
        **f,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def test_given_no_token_when_health_requested_then_200():
    # Act
    resp = client.get("/health")

    # Assert
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "assessment_service"}


@pytest.fixture
def startup(monkeypatch):
    """Point startup at a fresh SQLite database; record consumer and resync calls."""
    startup_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    calls = []
    monkeypatch.setattr(main, "engine", startup_engine)
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=startup_engine))
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("consumer"))
    monkeypatch.setattr(
        main.messaging, "resync_later", lambda task: calls.append(("resync", task))
    )
    yield startup_engine, calls
    startup_engine.dispose()


def test_given_service_starting_when_startup_runs_then_tables_created_badges_seeded_and_listeners_started(
    startup,
):
    # Arrange
    startup_engine, calls = startup

    # SQLite rejects "ADD COLUMN IF NOT EXISTS"; startup logs it and carries on.
    main.on_startup()

    session = sessionmaker(bind=startup_engine)()

    # Act and assert
    assert session.query(Badge).count() == len(badges.BADGE_DEFINITIONS)
    session.close()

    # Act and assert
    assert calls == ["consumer", ("resync", resync.announce_everything)]


def test_given_database_and_seed_failures_when_startup_runs_then_service_still_starts(
    startup, monkeypatch
):
    # Arrange
    _, calls = startup

    def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    monkeypatch.setattr(main, "seed_badges", broken)

    # Act
    main.on_startup()

    # Assert
    assert calls[0] == "consumer"


def test_given_consumer_when_started_then_binds_assessment_queue(monkeypatch):
    # Arrange
    calls = []
    monkeypatch.setattr(
        event_consumer.messaging, "start_consumer", lambda *args: calls.append(args)
    )

    # Act
    event_consumer.start()

    # Assert
    assert calls == [
        (
            "assessment",
            "assessment.queue",
            ev.QUEUES["assessment.queue"],
            event_consumer.handle,
        )
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
        event_consumer.handle("course.upserted", {})

    # Act and assert
    assert fake.rolled_back and fake.closed


# ---------------------------------------------------------------------------
# Listing and reading exams
# ---------------------------------------------------------------------------


def test_given_no_filter_when_listing_assessments_then_422_filter_required():
    # Act
    resp = client.get("/", headers=STUDENT)

    # Assert
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "FILTER_REQUIRED"


def test_given_draft_assessments_when_students_and_visitors_list_then_drafts_are_hidden(
    db,
):
    # Arrange
    _exam(db, title="Live", position=1)
    _exam(db, title="Draft", is_published=False, position=2)
    _exam(db, title="Other course", course_id=11)

    # Act
    titles = lambda headers: [  # noqa: E731
        a["title"] for a in client.get("/?course_id=10", headers=headers).json()
    ]

    # Assert
    assert titles(STUDENT) == ["Live"]
    assert titles({}) == ["Live"]
    assert titles(OWNER) == ["Live", "Draft"]
    assert titles(ADMIN) == ["Live", "Draft"]


def test_given_draft_assessment_when_read_then_student_gets_404_and_teacher_gets_200(
    db,
):
    # Arrange
    draft = _exam(db, is_published=False)
    _question(db, draft.id)

    # Act and assert
    assert client.get(f"/{draft.id}", headers=STUDENT).status_code == 404

    # Act
    resp = client.get(f"/{draft.id}", headers=OWNER)

    # Assert
    assert resp.status_code == 200
    assert resp.json()["question_count"] == 1


def test_given_no_token_when_assessment_fetched_then_401(db):
    # Arrange
    exam = _exam(db)

    # Act and assert
    assert client.get(f"/{exam.id}").status_code == 401


# ---------------------------------------------------------------------------
# Exam authoring
# ---------------------------------------------------------------------------


def test_given_minimal_exam_when_created_then_defaults_apply_and_naive_start_is_treated_as_utc(
    db, sent
):
    # Arrange
    _course(db)

    # Act
    resp = client.post(
        "/",
        json={
            "course_id": 10,
            "module_id": 7,
            "title": "Unit 1",
            "assessment_type": "exam",
            "scheduled_start": "2026-12-01T09:00:00",
        },
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 201

    # Act
    data = resp.json()

    # Assert
    assert data["assessment_type"] == "exam"
    assert data["is_published"] is False
    assert data["pass_score"] == 50 and data["question_count"] == 0
    assert data["scheduled_start"].startswith("2026-12-01T09:00:00")
    assert sent[-1][0] == ev.ASSESSMENT_UPSERTED
    assert sent[-1][1]["module_id"] == 7


def test_given_student_or_no_token_when_creating_exam_then_403_or_401_and_teacher_succeeds(
    db,
):
    # Arrange
    _course(db)
    body = {"course_id": 10, "title": "Unit 1"}

    # Act and assert
    assert client.post("/", json=body, headers=STUDENT).status_code == 403
    assert client.post("/", json=body).status_code == 401
    # An admin needs no local copy of the course at all.
    assert (
        client.post("/", json={**body, "course_id": 999}, headers=ADMIN).status_code
        == 201
    )


def test_given_unknown_course_when_teacher_creates_exam_then_404_course_not_found():
    # Act
    resp = client.post("/", json={"course_id": 999, "title": "x"}, headers=OWNER)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "COURSE_NOT_FOUND"


def test_given_owner_when_updating_exam_then_changes_are_saved(db, sent):
    # Arrange
    _course(db)
    exam = _exam(db)
    _question(db, exam.id)

    # Act
    resp = client.put(
        f"/{exam.id}",
        json={
            "assessment_type": "exam",
            "scheduled_start": "2026-12-01T09:00:00",
            "title": "Renamed",
        },
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()

    # Assert
    assert (data["assessment_type"], data["title"], data["question_count"]) == (
        "exam",
        "Renamed",
        1,
    )
    assert sent[-1][1]["title"] == "Renamed"


def test_given_unknown_exam_or_non_owner_when_updating_then_404_or_403_not_course_owner(
    db,
):
    # Arrange
    _course(db)
    exam = _exam(db)

    # Act and assert
    assert client.put("/999", json={"title": "x"}, headers=OWNER).status_code == 404

    # Act
    resp = client.put(f"/{exam.id}", json={"title": "x"}, headers=OTHER_TEACHER)

    # Assert
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "NOT_COURSE_OWNER"


def test_given_exam_with_questions_when_deleted_then_questions_are_removed(db, sent):
    # Arrange
    _course(db)
    exam = _exam(db)
    _question(db, exam.id)
    exam_id = exam.id

    # Act and assert
    assert client.delete(f"/{exam_id}", headers=OWNER).status_code == 204

    # Act and assert
    assert db.query(Question).count() == 0
    assert sent[-1] == (ev.ASSESSMENT_DELETED, {"id": exam_id})

    # Act and assert
    assert client.delete(f"/{exam_id}", headers=OWNER).status_code == 404


def test_given_lesson_with_exams_when_all_exams_deleted_then_204_and_exams_are_removed(
    db, sent
):
    # Arrange
    _course(db)
    a = _exam(db, module_id=7)
    b = _exam(db, module_id=7)
    keep_other_lesson = _exam(db, module_id=8)
    keep_other_course = _exam(db, course_id=11, module_id=7)
    _question(db, a.id)
    _question(db, keep_other_lesson.id)
    ids = {a.id, b.id}

    # Act
    resp = client.delete("/by-module/7?course_id=10", headers=OWNER)

    # Assert
    assert resp.status_code == 204

    remaining = {e.id for e in db.query(Assessment).all()}

    # Act and assert
    assert remaining == {keep_other_lesson.id, keep_other_course.id}
    assert db.query(Question).count() == 1
    assert {p["id"] for k, p in sent if k == ev.ASSESSMENT_DELETED} == ids


def test_given_lesson_without_exams_when_deleted_then_nothing_is_announced(db, sent):
    # Arrange
    _course(db)

    # Act and assert
    assert client.delete("/by-module/7?course_id=10", headers=OWNER).status_code == 204

    # Act and assert
    assert sent == []


def test_given_non_owner_when_deleting_lesson_exams_then_403(db):
    # Arrange
    _course(db)

    # Act and assert
    assert (
        client.delete("/by-module/7?course_id=10", headers=OTHER_TEACHER).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# Question authoring
# ---------------------------------------------------------------------------


def test_given_owner_when_questions_listed_then_answers_are_included_and_non_owner_is_403(
    db,
):
    # Arrange
    _course(db)
    exam = _exam(db)
    _question(db, exam.id, text="Second", position=2)
    _question(db, exam.id, text="First", position=1, answer="B")

    # Act
    resp = client.get(f"/{exam.id}/questions", headers=OWNER)

    # Assert
    assert resp.status_code == 200
    assert [q["question_text"] for q in resp.json()] == ["First", "Second"]
    assert resp.json()[0]["correct_answer"] == "B"
    assert resp.json()[0]["option_images"] == [None, None]

    # Act and assert
    assert client.get(f"/{exam.id}/questions", headers=STUDENT).status_code == 403
    assert client.get(f"/{exam.id}/questions", headers=OTHER_TEACHER).status_code == 403


def test_given_options_with_a_correct_flag_when_question_created_then_answer_is_taken_from_the_options(
    db,
):
    # Arrange
    _course(db)
    exam = _exam(db)

    # Act
    resp = client.post(
        f"/{exam.id}/questions",
        json={
            "question_text": "2 + 2?",
            "options": [
                {"option_text": "3"},
                {
                    "option_text": "4",
                    "is_correct": True,
                    "option_image_url": "https://img/4.png",
                    "option_image_public_id": "pid4",
                },
            ],
            "explanation": "Arithmetic",
        },
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 201

    # Act
    data = resp.json()

    # Assert
    assert data["options"] == ["3", "4"]
    assert data["correct_answer"] == "4"
    assert data["option_images"] == [
        None,
        {"url": "https://img/4.png", "public_id": "pid4"},
    ]


def test_given_explicit_answer_and_no_images_when_question_created_then_question_is_saved_without_images(
    db,
):
    # Arrange
    _course(db)
    exam = _exam(db)

    # Act
    resp = client.post(
        f"/{exam.id}/questions",
        json={
            "question_text": "Python is compiled?",
            "question_type": "true_false",
            "options": [{"option_text": "True"}, {"option_text": "False"}],
            "correct_answer": "False",
        },
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 201
    assert resp.json()["question_type"] == "true_false"
    assert resp.json()["correct_answer"] == "False"

    # Act
    q = db.get(Question, resp.json()["id"])

    # Assert
    assert q.option_images_json is None


def test_given_question_without_correct_answer_when_created_then_422_correct_answer_required(
    db,
):
    # Arrange
    _course(db)
    exam = _exam(db)

    # Act
    resp = client.post(
        f"/{exam.id}/questions",
        json={"question_text": "?", "options": [{"option_text": "x"}]},
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "CORRECT_ANSWER_REQUIRED"


def test_given_unknown_exam_when_question_created_then_404():
    # Act
    resp = client.post(
        "/999/questions",
        json={"question_text": "?", "correct_answer": "x"},
        headers=ADMIN,
    )

    # Assert
    assert resp.status_code == 404


def test_given_new_options_when_question_updated_then_options_set_the_answer(db):
    # Arrange
    _course(db)
    exam = _exam(db)
    q = _question(db, exam.id, options=("A", "B"), answer="A")

    # Act
    resp = client.put(
        f"/questions/{q.id}",
        json={
            "options": [
                {"option_text": "X", "is_correct": False},
                {"option_text": "Y", "is_correct": True},
            ],
            "question_type": "true_false",
        },
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()

    # Assert
    assert (data["options"], data["correct_answer"], data["question_type"]) == (
        ["X", "Y"],
        "Y",
        "true_false",
    )


@pytest.mark.xfail(
    strict=True,
    raises=KeyError,
    reason="BUG: update_question reads o['is_correct'], but model_dump(exclude_unset=True) "
    "drops it from options that omit it (schema default False) -> 500",
)
def test_given_options_without_correct_flag_when_question_updated_then_update_succeeds(
    db,
):
    # Arrange
    _course(db)
    exam = _exam(db)
    q = _question(db, exam.id)

    # Act
    resp = client.put(
        f"/questions/{q.id}",
        json={
            "options": [
                {"option_text": "X"},
                {"option_text": "Y", "is_correct": True},
            ]
        },
        headers=OWNER,
    )

    # Assert
    assert resp.status_code == 200
    assert resp.json()["correct_answer"] == "Y"


def test_given_explicit_answer_and_options_when_question_updated_then_explicit_answer_wins(
    db,
):
    # Arrange
    _course(db)
    exam = _exam(db)
    q = _question(db, exam.id)

    # Act
    resp = client.put(
        f"/questions/{q.id}",
        json={
            "options": [
                {"option_text": "X", "is_correct": True},
                {"option_text": "Y", "is_correct": False},
            ],
            "correct_answer": "Y",
        },
        headers=OWNER,
    )

    # Assert
    assert resp.json()["correct_answer"] == "Y"


def test_given_empty_options_when_question_updated_then_options_are_cleared(db):
    # Arrange
    _course(db)
    exam = _exam(db)
    q = _question(db, exam.id)

    # Act
    resp = client.put(
        f"/questions/{q.id}",
        json={"options": [], "question_type": "short_answer", "correct_answer": "x"},
        headers=OWNER,
    )

    # Assert
    assert resp.json()["options"] == []
    db.refresh(q)

    # Act and assert
    assert q.options_json is None and q.option_images_json is None


def test_given_unknown_question_or_non_owner_when_updating_or_deleting_then_404_or_403(
    db,
):
    # Arrange
    _course(db)
    exam = _exam(db)
    q = _question(db, exam.id)

    # Act and assert
    assert client.put("/questions/999", json={}, headers=OWNER).status_code == 404

    # Act
    resp = client.delete("/questions/999", headers=OWNER)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "QUESTION_NOT_FOUND"

    # Act and assert
    assert (
        client.put(f"/questions/{q.id}", json={}, headers=OTHER_TEACHER).status_code
        == 403
    )
    assert client.delete(f"/questions/{q.id}", headers=OTHER_TEACHER).status_code == 403


def test_given_question_when_deleted_then_204(db):
    # Arrange
    _course(db)
    exam = _exam(db)
    q = _question(db, exam.id)

    # Act and assert
    assert client.delete(f"/questions/{q.id}", headers=OWNER).status_code == 204

    # Act and assert
    assert db.query(Question).count() == 0


def _upload_csv(exam_id, content: bytes, headers=ADMIN, name="q.csv"):
    return client.post(
        f"/{exam_id}/questions/import-csv",
        files={"file": (name, content, "text/csv")},
        headers=headers,
    )


def test_given_oversized_csv_when_imported_then_413(db, monkeypatch):
    # Arrange
    exam = _exam(db)
    monkeypatch.setattr(router, "_MAX_CSV_SIZE", 10)

    # Act
    resp = _upload_csv(exam.id, b"question_text,option_a\n" * 5)

    # Assert
    assert resp.status_code == 413


def test_given_csv_in_wrong_encoding_when_imported_then_422_invalid_encoding(db):
    # Arrange
    exam = _exam(db)

    # Act
    resp = _upload_csv(exam.id, "question_text\nCafé".encode("latin-1"))

    # Assert
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "INVALID_ENCODING"


def test_given_csv_with_only_bad_rows_when_imported_then_nothing_is_created(db):
    # Arrange
    exam = _exam(db)

    # Act
    resp = _upload_csv(
        exam.id,
        b"question_text,option_a,option_b,correct_answer\n,A,B,A\nQ,A,B,Z\n",
    )

    # Assert
    assert resp.status_code == 201

    # Act
    data = resp.json()

    # Assert
    assert data["questions_created"] == 0
    assert data["errors"] == [
        "Row 2: question_text is empty",
        "Row 3: correct_answer must be one of A, B",
    ]


def test_given_non_owner_when_importing_csv_then_403(db):
    # Arrange
    _course(db)
    exam = _exam(db)

    # Act
    resp = _upload_csv(exam.id, b"question_text\n", headers=OTHER_TEACHER)

    # Assert
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Question images ZIP
# ---------------------------------------------------------------------------


def _upload_zip(url, data: bytes, headers=ADMIN, name="images.zip"):
    return client.post(
        url, files={"file": (name, data, "application/zip")}, headers=headers
    )


def test_given_images_zip_when_uploaded_then_question_and_option_images_are_attached(
    db,
):
    # Arrange
    exam = _exam(db)
    first = _question(db, exam.id, options=("A", "B", "C"), position=1)
    second = _question(db, exam.id, position=2)
    bundle = _zip(
        {
            "imgs/1-0.png": PNG,  # question 1 image (folders are ignored)
            "1-2.PNG": PNG,  # question 1, option B
            "2-0.jpg": PNG,
            "notes.txt": "skip me",
            "3-0.png": PNG,  # there is no question 3
            "2-5.png": PNG,  # question 2 has two options
            "1-1-bad.png": PNG,  # wrong name pattern: skipped
            "2-1.png": b"",
        }
    )

    # Act
    resp = _upload_zip(f"/{exam.id}/questions/images-zip", bundle)

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()

    # Assert
    assert data["applied"] == 4
    assert data["skipped"] == 2
    assert data["errors"] == [
        "2-5.png: question 2 has no option 5",
        "3-0.png: there is no question 3",
    ]
    assert data["message"] == "Applied 4 image(s), skipped 2 file(s) with other names"

    db.expire_all()

    # Act
    first, second = db.get(Question, first.id), db.get(Question, second.id)

    # Assert
    assert first.question_image_url == "https://img/1-0.png"
    assert json.loads(first.option_images_json) == [
        None,
        {"url": "https://img/1-2.PNG", "public_id": "pid-1-2.PNG"},
        None,
    ]
    assert second.question_image_public_id == "pid-2-0.jpg"
    assert json.loads(second.option_images_json)[0]["url"] == "https://img/2-1.png"


def test_given_failing_image_upload_when_zip_processed_then_failures_are_reported(db):
    # Arrange
    exam = _exam(db)
    _question(db, exam.id)

    async def fail(data, filename):
        raise ValueError("Cloudinary credentials are not set")

    bulk_import.upload_image = fail  # restored by the autouse `uploads` fixture

    # Act
    resp = _upload_zip(f"/{exam.id}/questions/images-zip", _zip({"1-0.png": PNG}))

    # Assert
    assert resp.json()["applied"] == 0
    assert resp.json()["errors"] == ["1-0.png: Cloudinary credentials are not set"]
    assert resp.json()["message"] == "Applied 0 image(s)"


def test_given_bad_zip_uploads_when_processed_then_415_422_or_413_is_returned(
    db, monkeypatch
):
    # Arrange
    exam = _exam(db)
    url = f"/{exam.id}/questions/images-zip"

    # Act and assert
    assert _upload_zip(url, b"x", name="images.rar").status_code == 415

    # Act
    bad = _upload_zip(url, b"not a zip")

    # Assert
    assert bad.status_code == 422
    assert bad.json()["detail"]["error"] == "INVALID_ZIP"

    monkeypatch.setattr(router, "_MAX_ZIP_SIZE", 10)

    # Act and assert
    assert _upload_zip(url, _zip({"1-0.png": PNG})).status_code == 413


def test_given_non_owner_or_unknown_exam_when_uploading_images_zip_then_403_or_404(db):
    # Arrange
    _course(db)
    exam = _exam(db)

    # Act
    resp = _upload_zip(
        f"/{exam.id}/questions/images-zip", _zip({}), headers=OTHER_TEACHER
    )

    # Assert
    assert resp.status_code == 403

    # Act and assert
    assert _upload_zip("/999/questions/images-zip", _zip({})).status_code == 404


def test_given_zip_exceeding_entry_or_size_limits_when_read_then_it_is_rejected(
    monkeypatch,
):
    # Arrange
    bundle = _zip({"a.txt": "x" * 100, "b.txt": "y"})

    # Act and assert
    assert len(bulk_import.safe_zip(bundle).infolist()) == 2

    monkeypatch.setattr(bulk_import, "MAX_ZIP_ENTRIES", 1)

    # Act and assert
    with pytest.raises(ValueError, match="more than 1 files"):
        bulk_import.safe_zip(bundle)

    monkeypatch.setattr(bulk_import, "MAX_ZIP_ENTRIES", 10)
    monkeypatch.setattr(bulk_import, "MAX_UNCOMPRESSED_BYTES", 50)

    # Act and assert
    with pytest.raises(ValueError, match="300 MB"):
        bulk_import.safe_zip(bundle)


def test_given_paths_with_either_slash_when_basename_taken_then_last_segment_is_returned():
    # Act and assert
    assert bulk_import._basename("a/b/1-0.png") == "1-0.png"
    assert bulk_import._basename("a\\b\\1-0.png") == "1-0.png"
    assert bulk_import._basename("1-0.png") == "1-0.png"


# ---------------------------------------------------------------------------
# Bulk exam import
# ---------------------------------------------------------------------------

QUESTIONS_CSV = (
    "question_text,option_a,option_b,option_c,correct_answer,explanation\n"
    "2 + 2?,3,4,5,B,\n"
    "Capital of France?,Paris,Rome,,A,\n"
    "Broken row,,,,A,\n"
)


def test_given_teacher_when_bulk_import_template_requested_then_a_valid_bundle_is_returned():
    # Act
    resp = client.get("/bulk-import/template", headers=OWNER)

    # Assert
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    # Act
    names = set(zipfile.ZipFile(io.BytesIO(resp.content)).namelist())

    # Assert
    assert {
        "exams.csv",
        "questions/exam1/questions.csv",
        "questions/exam2/questions.csv",
        "README.md",
    } <= names

    # Act and assert
    assert client.get("/bulk-import/template", headers=STUDENT).status_code == 403


def test_given_bundle_with_good_and_bad_exams_when_bulk_imported_then_exams_created_and_problems_reported(
    db, sent
):
    # Arrange
    _course(db)
    _exam(db, module_id=7, position=4)  # new exams are placed after this one
    exams_csv = (
        "folder,title,description,duration_minutes,order_number,is_published,"
        "scheduled_start,thumbnail\n"
        "exam1,Paper 1,First paper,90,,false,2026-12-01 09:00,exam1.png\n"
        "exam2,Paper 2,,,2,,,missing.png\n"
        "exam3,Paper 3,,,,yes,,bad.png\n"
        ",No folder,,,,,,\n"
        "exam4,No questions file,,,,,,\n"
        "exam5,Bad start,,,,,next week,\n"
        "exam6,Bad duration,,ninety,,,,\n"
    )
    bundle = _zip(
        {
            "bundle/exams.csv": "﻿" + exams_csv,
            "bundle/thumbnails/exam1.png": PNG,
            "bundle/thumbnails/bad.png": PNG,
            "bundle/questions/exam1/questions.csv": QUESTIONS_CSV,
            "bundle/questions/exam1/images/1-0.png": PNG,
            "bundle/questions/exam1/images/9-0.png": PNG,
            "bundle/questions/exam2/questions.csv": QUESTIONS_CSV,
            "bundle/questions/exam3/questions.csv": QUESTIONS_CSV,
            "bundle/questions/exam5/questions.csv": QUESTIONS_CSV,
            "bundle/questions/exam6/questions.csv": QUESTIONS_CSV,
        }
    )

    # Act
    resp = _upload_zip("/bulk-import?course_id=10&module_id=7", bundle, headers=OWNER)

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()

    # Assert
    assert data["created_exams"] == 3
    assert data["total_questions_created"] == 6
    assert data["skipped_exams"] == 4
    assert [e["exam"] for e in data["errors"]] == [
        "No folder",
        "No questions file",
        "Bad start",
        "Bad duration",
    ]
    assert data["errors"][1]["message"] == "questions/exam4/questions.csv is missing"
    assert "2026-08-01 09:00" in data["errors"][2]["message"]

    paper1, paper2, paper3 = data["details"]

    # Act and assert
    assert paper1["images_applied"] == 1
    assert paper1["warnings"] == [
        "Row 4: at least option_a and option_b are required",
        "9-0.png: there is no question 9",
    ]
    assert "thumbnail missing.png not found" in paper2["warnings"]
    assert any(w.startswith("thumbnail bad.png:") for w in paper3["warnings"])

    # Act
    exam1 = db.get(Assessment, int(paper1["exam_id"]))

    # Assert
    assert exam1.is_published is False
    assert exam1.time_limit_minutes == 90
    assert exam1.image_url == "https://img/exam1.png"
    assert exam1.module_id == 7
    assert exam1.position == 5  # after the existing exam
    # SQLite drops the +05:30 offset and keeps the wall time; the offset itself is
    # checked in test_parse_helpers.
    assert exam1.scheduled_start.replace(tzinfo=None) == datetime(2026, 12, 1, 9, 0)

    # Act
    exam2 = db.get(Assessment, int(paper2["exam_id"]))

    # Assert
    assert (exam2.position, exam2.time_limit_minutes, exam2.is_published) == (
        2,
        60,
        True,
    )
    assert exam2.description is None

    announced = {p["id"] for k, p in sent if k == ev.ASSESSMENT_UPSERTED}

    # Act and assert
    assert announced == {int(d["exam_id"]) for d in data["details"]}


def test_given_bundle_without_exams_csv_when_bulk_imported_then_422_invalid_bundle(db):
    # Act
    resp = _upload_zip(
        "/bulk-import?course_id=10", _zip({"readme.txt": "x"}), headers=ADMIN
    )

    # Assert
    assert resp.status_code == 422
    assert resp.json()["detail"] == {
        "error": "INVALID_BUNDLE",
        "message": "exams.csv was not found in the ZIP",
    }


def test_given_deeply_nested_exams_csv_when_bulk_imported_then_it_is_ignored_and_422_returned(
    db,
):
    # Arrange
    bundle = _zip({"a/b/exams.csv": "folder,title\n"})

    # Act
    resp = _upload_zip("/bulk-import?course_id=10", bundle, headers=ADMIN)

    # Assert
    assert resp.status_code == 422


def test_given_non_zip_or_non_owner_when_bulk_importing_then_415_or_403(db):
    # Arrange
    _course(db)

    # Act and assert
    assert (
        _upload_zip("/bulk-import?course_id=10", b"x", name="b.txt").status_code == 415
    )
    assert (
        _upload_zip(
            "/bulk-import?course_id=10", _zip({}), headers=OTHER_TEACHER
        ).status_code
        == 403
    )


def test_given_values_when_parse_helpers_run_then_valid_ones_parse_and_invalid_ones_raise():
    # Act and assert
    assert bulk_import._parse_bool("", True) is True
    assert bulk_import._parse_bool(" YES ", False) is True
    assert bulk_import._parse_bool("no", True) is False
    assert bulk_import._parse_start("") is None
    assert bulk_import._parse_start("2026-08-01").hour == 0
    assert bulk_import._parse_start("2026-08-01T09:30").minute == 30
    assert bulk_import._parse_start("2026-08-01 09:00").utcoffset() == timedelta(
        hours=5, minutes=30
    )

    # Act and assert
    with pytest.raises(ValueError):
        bulk_import._parse_start("01/08/2026")


def test_given_existing_questions_when_rows_imported_then_positions_continue(db):
    # Arrange
    exam = _exam(db)
    _question(db, exam.id, position=7)

    # Act
    created, errors = bulk_import.import_question_rows(
        db,
        exam.id,
        "question_text,option_a,option_b,correct_answer,explanation\nQ,A,B,b,Why\n",
    )
    db.commit()

    # Assert
    assert (created, errors) == (1, [])

    # Act
    q = db.query(Question).filter(Question.position == 8).one()

    # Assert
    assert q.correct_answer == "B" and q.explanation == "Why"


# ---------------------------------------------------------------------------
# images.upload_image
# ---------------------------------------------------------------------------


def test_given_image_when_uploaded_then_url_and_public_id_are_returned(monkeypatch):
    # Arrange
    calls = []

    def upload(data, filename, folder, resource_type):
        calls.append((filename, folder, resource_type))
        return "pid-1", "https://img/1.png"

    monkeypatch.setattr(media, "upload", upload)

    # Act and assert
    assert run(images.upload_image(PNG, "1.png")) == ("https://img/1.png", "pid-1")
    assert calls == [("1.png", media.UPLOAD_FOLDER, "image")]


def test_given_non_image_when_uploaded_then_it_is_refused():
    # Act and assert
    with pytest.raises(ValueError, match="not allowed"):
        run(images.upload_image(b"%PDF-1.4", "doc.png"))
    with pytest.raises(ValueError, match="empty"):
        run(images.upload_image(b"", "x.png"))


def test_given_storage_error_when_image_uploaded_then_error_is_wrapped(monkeypatch):
    # Arrange
    def upload(*args):
        raise media.MediaError(503, "CLOUDINARY_NOT_CONFIGURED", "not configured")

    monkeypatch.setattr(media, "upload", upload)

    # Act and assert
    with pytest.raises(ValueError, match="not configured"):
        run(images.upload_image(PNG, "1.png"))


# ---------------------------------------------------------------------------
# Taking an exam — start, submit, last attempt
# ---------------------------------------------------------------------------


def test_given_draft_assessment_when_student_starts_it_then_404(db):
    # Arrange
    draft = _exam(db, is_published=False)

    # Act
    resp = client.post(f"/{draft.id}/start", headers=STUDENT)

    # Assert
    assert resp.status_code == 404


def test_given_draft_assessment_when_teacher_previews_it_then_session_starts_without_enrollment(
    db, monkeypatch
):
    # Arrange
    async def never(*args):
        raise AssertionError("staff previews skip the enrollment check")

    monkeypatch.setattr(router, "is_enrolled", never)
    draft = _exam(
        db,
        is_published=False,
        scheduled_start=datetime.now(timezone.utc) + timedelta(days=1),
    )

    # Act and assert
    assert client.post(f"/{draft.id}/start", headers=OWNER).status_code == 200


def test_given_assessment_without_time_limit_when_started_then_session_lasts_an_hour(
    db,
):
    # Arrange
    exam = _exam(db, time_limit_minutes=None)
    _question(
        db,
        exam.id,
        options=("A", "B"),
        option_images_json=json.dumps([{"url": "https://img/a.png"}]),
    )

    # Act
    resp = client.post(f"/{exam.id}/start", headers=STUDENT)

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()

    # Assert
    assert data["time_remaining_seconds"] == 3600
    assert data["questions"][0]["option_image_urls"] == ["https://img/a.png", None]


def test_given_expired_session_when_started_again_then_a_new_session_is_created(
    db, redis
):
    # Arrange
    exam = _exam(db)
    redis.set(f"session:active:101:{exam.id}", "sess_old")  # session data already gone

    # Act
    resp = client.post(f"/{exam.id}/start", headers=STUDENT)

    # Assert
    assert resp.status_code == 200
    assert resp.json()["session_id"] != "sess_old"


def test_given_scheduled_start_in_the_past_when_started_then_assessment_is_open(db):
    # Arrange
    exam = _exam(db, scheduled_start=datetime(2020, 1, 1))

    # Act and assert
    assert client.post(f"/{exam.id}/start", headers=STUDENT).status_code == 200


def test_given_active_session_when_submitted_then_attempt_is_graded_and_session_cleared(
    db, redis
):
    # Arrange
    exam = _exam(db, pass_score=60)
    q1 = _question(db, exam.id, options=("def", "func"), answer="def", marks=2)
    q2 = _question(
        db,
        exam.id,
        options=("True", "False"),
        answer="False",
        position=2,
        question_type=QuestionType.true_false,
    )
    q3 = _question(
        db,
        exam.id,
        options=None,
        answer="anything",
        position=3,
        question_type=QuestionType.short_answer,
    )
    session_id = client.post(f"/{exam.id}/start", headers=STUDENT).json()["session_id"]

    # Act
    resp = client.post(
        f"/{exam.id}/submit",
        json={
            "session_id": session_id,
            "answers": [
                {"question_id": q1.id, "selected_option": " DEF "},
                {"question_id": q2.id, "selected_option": "True"},
                {"question_id": q3.id, "selected_option": "anything"},
            ],
        },
        headers=STUDENT,
    )

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()
    # 2 of 4 marks; short answers wait for manual review.
    # Assert
    assert (data["score"], data["passed"], data["correct_count"]) == (50.0, False, 1)
    assert data["auto_submitted"] is False
    review = {r["question_id"]: r for r in data["review"]}

    # Act and assert
    assert review[q1.id]["is_correct"] is True
    assert review[q2.id]["is_correct"] is False
    assert review[q2.id]["selected_option_id"] == "True"
    assert review[q3.id]["is_correct"] is False and review[q3.id]["options"] == []
    assert data["ranking"]["exam_title"] == "Quiz"

    # Act and assert
    assert redis.get(f"session:assessment:{session_id}") is None
    assert redis.get(f"session:active:101:{exam.id}") is None

    # Act and assert
    assert db.query(Submission).count() == 1


def test_given_expired_session_when_submitted_then_the_open_attempt_is_reused(
    db, redis
):
    # Arrange
    exam = _exam(db)
    _question(db, exam.id)
    session_id = client.post(f"/{exam.id}/start", headers=STUDENT).json()["session_id"]
    redis.delete(f"session:assessment:{session_id}")  # the timer ran out

    # Act
    resp = client.post(
        f"/{exam.id}/submit", json={"session_id": session_id}, headers=STUDENT
    )

    # Assert
    assert resp.json()["auto_submitted"] is True
    assert resp.json()["score"] == 0.0

    # Act
    (sub,) = db.query(Submission).all()

    # Assert
    assert sub.status == SubmissionStatus.graded


def test_given_exam_without_questions_when_submitted_then_score_is_zero(db):
    # Arrange
    exam = _exam(db)

    # Act
    resp = client.post(f"/{exam.id}/submit", json={"session_id": "x"}, headers=STUDENT)

    # Assert
    assert resp.status_code == 200
    assert (resp.json()["score"], resp.json()["total_questions"]) == (0.0, 0)


def test_given_unknown_exam_when_submitted_then_404(db):
    # Act
    resp = client.post("/999/submit", json={"session_id": "x"}, headers=STUDENT)

    # Assert
    assert resp.status_code == 404


def test_given_badge_awarding_fails_when_submitted_then_submission_still_succeeds(
    db, monkeypatch
):
    # Arrange
    exam = _exam(db)
    _question(db, exam.id)

    def broken(*args):
        raise RuntimeError("badge table locked")

    monkeypatch.setattr(router, "evaluate_after_attempt", broken)

    # Act
    resp = client.post(f"/{exam.id}/submit", json={"session_id": "x"}, headers=STUDENT)

    # Assert
    assert resp.status_code == 200
    assert resp.json()["new_badges"] == []


def test_given_new_badges_earned_when_submitted_then_response_lists_them(
    db, seeded_badges
):
    # Arrange
    exam = _exam(db)
    q = _question(db, exam.id)
    resp = client.post(
        f"/{exam.id}/submit",
        json={
            "session_id": "x",
            "answers": [{"question_id": q.id, "selected_option": "A"}],
        },
        headers=STUDENT,
    )

    # Act
    keys = {b["criteria_key"] for b in resp.json()["new_badges"]}

    # Assert
    assert {"perfect_100", "first_step", "speed_demon"} <= keys


def test_given_graded_attempts_when_last_attempt_requested_then_latest_attempt_returned(
    db,
):
    # Arrange
    exam = _exam(db, pass_score=50)
    q = _question(db, exam.id, answer="A")
    old = datetime.now(timezone.utc) - timedelta(days=1)
    _graded(db, exam.id, score=0, when=old, answers_json="[]")
    _graded(
        db,
        exam.id,
        score=100,
        seconds=90,
        answers_json=json.dumps([{"question_id": q.id, "selected_option": "A"}]),
    )

    # Act
    resp = client.get(f"/{exam.id}/my-last-attempt", headers=STUDENT)

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()

    # Assert
    assert (data["score"], data["passed"], data["correct_count"]) == (100.0, True, 1)
    assert data["time_taken_seconds"] == 90
    assert data["session_id"] == ""


def test_given_no_attempt_when_last_attempt_requested_then_404_no_attempt_found(db):
    # Arrange
    exam = _exam(db)

    # Act
    resp = client.get(f"/{exam.id}/my-last-attempt", headers=STUDENT)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "NO_ATTEMPT_FOUND"


def test_my_last_attempt_is_served_on_the_api_prefixed_path(db):
    # The same handler is registered a second time under /api/assessments/...
    exam = _exam(db, pass_score=50)
    q = _question(db, exam.id, answer="A")
    _graded(
        db,
        exam.id,
        score=100,
        seconds=90,
        answers_json=json.dumps([{"question_id": q.id, "selected_option": "A"}]),
    )

    prefixed = client.get(
        f"/api/assessments/{exam.id}/my-last-attempt", headers=STUDENT
    )
    plain = client.get(f"/{exam.id}/my-last-attempt", headers=STUDENT)

    assert prefixed.status_code == 200
    assert prefixed.json() == plain.json()
    assert prefixed.json()["score"] == 100.0


def test_my_last_attempt_api_prefixed_path_404_without_one(db):
    exam = _exam(db)
    resp = client.get(f"/api/assessments/{exam.id}/my-last-attempt", headers=STUDENT)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "NO_ATTEMPT_FOUND"


def test_my_last_attempt_api_prefixed_path_requires_a_token(db):
    exam = _exam(db)
    resp = client.get(f"/api/assessments/{exam.id}/my-last-attempt")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# My attempts, my submissions, attempt review
# ---------------------------------------------------------------------------


def test_given_attempts_when_my_attempts_requested_then_summary_per_exam_returned(db):
    # Arrange
    a = _exam(db, pass_score=60)
    b = _exam(db, pass_score=60)
    _exam(db, course_id=11)
    now = datetime.now(timezone.utc)
    _graded(db, a.id, score=70, when=now - timedelta(hours=2))
    _graded(db, a.id, score=40, when=now)  # latest, failed
    _graded(db, b.id, score=10)
    _graded(db, a.id, student_id=102, score=100)  # someone else

    resp = client.get("/my-attempts?course_id=10", headers=STUDENT)

    # Act
    summary = {s["assessment_id"]: s for s in resp.json()}

    # Assert
    assert summary[a.id] == {
        "assessment_id": a.id,
        "attempt_count": 2,
        "last_score": 40.0,
        "passed": True,  # an earlier attempt passed
    }
    assert summary[b.id]["passed"] is False


def test_given_course_without_exams_when_my_attempts_requested_then_empty_list_returned():
    # Act and assert
    assert client.get("/my-attempts?course_id=99", headers=STUDENT).json() == []


def test_given_attempts_when_my_submissions_requested_then_listed_newest_first(db):
    # Arrange
    _course(db, title="Python Basics")
    _student(db, 101, district="Colombo")
    exam = _exam(db, module_id=7)
    gone = _exam(db)
    q = _question(db, exam.id, answer="A")
    now = datetime.now(timezone.utc)
    first = _graded(db, exam.id, score=0, when=now - timedelta(hours=1))
    second = _graded(
        db,
        exam.id,
        score=100,
        when=now,
        answers_json=json.dumps([{"question_id": q.id, "selected_option": "A"}]),
    )
    _graded(db, gone.id, when=now - timedelta(minutes=30))
    db.delete(gone)
    db.commit()

    # Act
    resp = client.get("/my-submissions", headers=STUDENT)

    # Assert
    assert resp.status_code == 200

    # Act
    items = resp.json()

    # Assert
    assert [i["attempt_id"] for i in items] == [second.id, first.id]
    assert [i["attempt_number"] for i in items] == [2, 1]
    newest = items[0]

    # Act and assert
    assert newest["course_title"] == "Python Basics"
    assert newest["sub_course_id"] == 7
    assert (newest["marks_obtained"], newest["total_questions"]) == (1, 1)
    # The board uses first attempts: this student's first try ranks them.
    assert (newest["overall_rank"], newest["district_rank"]) == (1, 1)


def test_given_no_attempts_or_bad_limit_when_my_submissions_requested_then_empty_list_or_422(
    db,
):
    # Act and assert
    assert client.get("/my-submissions", headers=STUDENT).json() == []
    assert client.get("/my-submissions?limit=0", headers=STUDENT).status_code == 422
    assert client.get("/my-submissions?limit=51", headers=STUDENT).status_code == 422


@pytest.fixture
def attempt(db):
    """Student 101's graded attempt on an exam in course 10 (owned by teacher 5)."""
    _course(db)
    _student(db, 101)
    exam = _exam(db, pass_score=50)
    q = _question(
        db,
        exam.id,
        options=("A", "B"),
        answer="B",
        explanation="Because",
        question_image_url="https://img/q.png",
    )
    return _graded(
        db,
        exam.id,
        score=100,
        answers_json=json.dumps([{"question_id": q.id, "selected_option": "B"}]),
    )


def test_given_graded_attempt_when_student_reviews_it_then_review_returned(attempt):
    # Act
    resp = client.get(f"/submissions/{attempt.id}/review", headers=STUDENT)

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()

    # Assert
    assert (data["score"], data["passed"], data["marks_obtained"]) == (100.0, True, 1)
    (item,) = data["review"]

    # Act and assert
    assert item["is_correct"] is True
    assert item["explanation"] == "Because"
    assert item["question_image_url"] == "https://img/q.png"
    assert [o["order_number"] for o in item["options"]] == [0, 1]
    assert data["ranking"]["course_title"] == "Python"
    assert data["ranking"]["overall_rank"] == 1


def test_given_attempt_when_reviewed_by_owner_teacher_or_other_student_then_visibility_rules_apply(
    attempt,
):
    # Arrange
    url = f"/submissions/{attempt.id}/review"
    # Another student must not even learn that the attempt exists.
    # Act and assert
    assert client.get(url, headers=OTHER_STUDENT).status_code == 404
    assert client.get(url, headers=ADMIN).status_code == 200
    assert client.get(url, headers=OWNER).status_code == 200
    assert client.get(url, headers=OTHER_TEACHER).status_code == 403


def test_given_missing_or_unfinished_attempt_when_reviewed_then_404(db, attempt):
    # Arrange
    open_attempt = Submission(
        assessment_id=attempt.assessment_id,
        student_id=101,
        status=SubmissionStatus.in_progress,
    )
    db.add(open_attempt)
    db.commit()

    # Act and assert
    assert client.get("/submissions/999/review", headers=STUDENT).status_code == 404
    assert (
        client.get(
            f"/submissions/{open_attempt.id}/review", headers=STUDENT
        ).status_code
        == 404
    )

    db.query(Assessment).delete()
    db.commit()

    # Act
    resp = client.get(f"/submissions/{attempt.id}/review", headers=STUDENT)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "ATTEMPT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Violations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("tab-switch", ViolationType.tab_switch),
        ("COPY-PASTE", ViolationType.copy_paste),
        ("multiple_faces", ViolationType.multiple_faces),
        ("multiple-faces", ViolationType.multiple_faces),
        ("no-face", ViolationType.no_face),
        ("no_face", ViolationType.no_face),
        ("looked away", ViolationType.other),
    ],
)
def test_given_violation_types_when_reported_then_204(db, raw, expected):
    # Arrange
    exam = _exam(db)

    # Act
    resp = client.post(
        f"/{exam.id}/violations",
        json={"session_id": "none", "violation_type": raw},
        headers=STUDENT,
    )

    # Assert
    assert resp.status_code == 204

    # Act
    log = db.query(ViolationLog).one()

    # Assert
    assert log.violation_type == expected
    assert log.detail == f"Violation recorded: {raw}"


def test_given_no_session_when_violation_reported_then_latest_attempt_is_used(db):
    # Arrange
    exam = _exam(db)
    _graded(db, exam.id)
    latest = _graded(db, exam.id)

    client.post(
        f"/{exam.id}/violations",
        json={
            "session_id": "none",
            "violation_type": "tab_switch",
            "detail": "Alt+Tab",
        },
        headers=STUDENT,
    )

    # Act
    log = db.query(ViolationLog).one()

    # Assert
    assert log.submission_id == latest.id
    assert log.detail == "Alt+Tab"


def test_given_no_attempt_when_violation_reported_then_an_attempt_is_opened(db):
    # Arrange
    exam = _exam(db)
    client.post(
        f"/{exam.id}/violations",
        json={"session_id": "none", "violation_type": "tab_switch"},
        headers=STUDENT,
    )

    sub = db.query(Submission).one()

    # Act and assert
    assert sub.status == SubmissionStatus.in_progress
    assert db.query(ViolationLog).one().submission_id == sub.id


def test_given_active_session_when_violation_reported_then_its_attempt_is_used(db):
    # Arrange
    exam = _exam(db)
    session_id = client.post(f"/{exam.id}/start", headers=STUDENT).json()["session_id"]
    _graded(db, exam.id)  # a newer row must not steal the violation

    client.post(
        f"/{exam.id}/violations",
        json={"session_id": session_id, "violation_type": "tab_switch"},
        headers=STUDENT,
    )

    started = db.query(Submission).filter_by(status=SubmissionStatus.in_progress).one()

    # Act and assert
    assert db.query(ViolationLog).one().submission_id == started.id


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------


def test_given_visitor_when_ranking_exams_requested_then_every_attempted_exam_is_listed(
    db,
):
    # Arrange
    _course(db, 10, title="Python")
    _course(db, 11, title="Biology")
    py = _exam(db, title="Loops")
    bio = _exam(db, course_id=11, title="Cells")
    draft = _exam(db, title="Draft", is_published=False)
    _exam(db, title="Never sat")
    for exam in (py, bio, draft):
        _graded(db, exam.id, student_id=102)

    # Act
    rows = client.get("/rankings/exams").json()

    # Assert
    assert [(r["course_title"], r["exam_title"]) for r in rows] == [
        ("Biology", "Cells"),
        ("Python", "Loops"),
    ]
    # A student only sees exams they sat themselves.
    # Act and assert
    assert client.get("/rankings/exams", headers=STUDENT).json() == []


def test_given_paging_or_unknown_exam_when_leaderboard_requested_then_pages_are_returned_and_unknown_exam_is_404(
    db,
):
    # Arrange
    exam = _exam(db)
    for i, uid in enumerate((101, 102, 103)):
        _student(db, uid)
        _graded(db, exam.id, student_id=uid, score=90 - i * 10)

    # Act
    page = client.get(f"/rankings/leaderboard?exam_id={exam.id}&limit=1&offset=1")

    # Assert
    assert [(r["rank"], r["full_name"]) for r in page.json()] == [(2, "Student 102")]

    # Act and assert
    assert client.get("/rankings/leaderboard?exam_id=999").status_code == 404
    assert (
        client.get(f"/rankings/leaderboard?exam_id={exam.id}&limit=0").status_code
        == 422
    )


def test_given_profiles_unavailable_when_leaderboard_requested_then_leaderboard_is_still_returned(
    db, monkeypatch
):
    # Arrange
    exam = _exam(db)
    _graded(db, exam.id, student_id=101, score=90)
    _graded(db, exam.id, student_id=900, score=95)

    async def no_profiles(ids):
        return None

    monkeypatch.setattr(router, "get_profiles", no_profiles)

    # Act
    rows = client.get(f"/rankings/leaderboard?exam_id={exam.id}").json()
    # Without profiles nobody can be filtered out, and everyone is shown unnamed.
    # Assert
    assert [(r["full_name"], r["district"], r["grade"]) for r in rows] == [
        ("Student", "", 0),
        ("Student", "", 0),
    ]


def test_given_student_who_has_not_sat_the_exam_when_rank_requested_then_rank_is_null(
    db,
):
    # Arrange
    _course(db)
    exam = _exam(db)

    # Act
    resp = client.get(f"/rankings/exam/{exam.id}", headers=STUDENT)

    # Assert
    assert resp.status_code == 200
    assert resp.json()["overall_rank"] is None
    assert resp.json()["course_title"] == "Python"

    # Act and assert
    assert client.get(f"/rankings/exam/{exam.id}").status_code == 401


# ---------------------------------------------------------------------------
# Badge routes
# ---------------------------------------------------------------------------


def test_given_no_token_when_badge_catalogue_requested_then_sorted_badges_returned(
    seeded_badges,
):
    # Act
    rows = client.get("/badges").json()

    # Assert
    assert len(rows) == len(badges.BADGE_DEFINITIONS)
    assert rows == sorted(rows, key=lambda b: (b["category"], b["name"]))


def test_given_student_when_badges_requested_then_own_badges_and_dashboard_returned_and_no_token_is_401(
    db, seeded_badges
):
    # Arrange
    trophy = db.query(Badge).filter_by(criteria_key="perfect_100").one()
    db.add(UserBadge(student_id=101, badge_id=trophy.id))
    db.add(UserBadge(student_id=102, badge_id=trophy.id))
    db.commit()

    # Act
    mine = client.get("/badges/my", headers=STUDENT).json()

    # Assert
    assert [(b["user_id"], b["badge"]["criteria_key"]) for b in mine] == [
        (101, "perfect_100")
    ]

    # Act
    dash = client.get("/badges/dashboard", headers=STUDENT).json()

    # Assert
    assert len(dash["available_badges"]) == len(badges.BADGE_DEFINITIONS)
    assert len(dash["earned_badges"]) == 1

    # Act and assert
    assert client.get("/badges/my").status_code == 401


# ---------------------------------------------------------------------------
# Badge rules
# ---------------------------------------------------------------------------


def _earned(db, sub, exam) -> set[str]:
    return {b.criteria_key for b in badges.evaluate_after_attempt(db, sub, exam)}


def _local_to_utc(hour, minute=0, day=1) -> datetime:
    """A Sri Lanka local time (UTC+05:30) on 2026-10-<day>, as UTC."""
    local = datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)
    return local - badges.LOCAL_OFFSET


def test_given_seeded_badges_when_seeding_runs_again_then_no_duplicates_are_created(db):
    # Arrange
    badges.seed_badges(db)
    badges.seed_badges(db)

    # Act and assert
    assert db.query(Badge).count() == len(badges.BADGE_DEFINITIONS)


def test_given_early_morning_attempt_when_badges_checked_then_early_bird_is_awarded(
    db, seeded_badges
):
    # Arrange
    exam = _exam(db)

    sub = _graded(db, exam.id, score=10, when=_local_to_utc(5, 30), seconds=600)

    # Act and assert
    assert "early_bird" in _earned(db, sub, exam)  # started 05:20 local


@pytest.mark.parametrize("hour", [23, 1])
def test_given_late_night_attempt_when_badges_checked_then_night_owl_is_awarded(
    db, seeded_badges, hour
):
    # Arrange
    exam = _exam(db)

    sub = _graded(db, exam.id, score=10, when=_local_to_utc(hour))

    # Act and assert
    assert "night_owl" in _earned(db, sub, exam)


def test_given_daytime_attempt_when_badges_checked_then_no_time_badges_are_awarded(
    db, seeded_badges
):
    # Arrange
    exam = _exam(db)

    sub = _graded(db, exam.id, score=10, when=_local_to_utc(12))

    # Act and assert
    assert not {"early_bird", "night_owl"} & _earned(db, sub, exam)


def test_given_seven_consecutive_days_of_attempts_when_badges_checked_then_consistency_king_is_awarded(
    db, seeded_badges
):
    # Arrange
    exam = _exam(db)
    now = datetime.now(timezone.utc)
    for days in range(1, 6):
        _graded(db, exam.id, score=10, when=now - timedelta(days=days))

    sub = _graded(db, exam.id, score=10, when=now)

    # Act and assert
    assert "consistency_king" not in _earned(db, sub, exam)  # six days

    _graded(db, exam.id, score=10, when=now - timedelta(days=6))

    sub = _graded(db, exam.id, score=10, when=now)

    # Act and assert
    assert "consistency_king" in _earned(db, sub, exam)


def test_given_attempt_finished_within_a_third_of_the_time_when_badges_checked_then_speed_demon_is_awarded(
    db, seeded_badges
):
    # Arrange
    exam = _exam(db, time_limit_minutes=15)  # a third is 300 seconds

    slow = _graded(db, exam.id, score=90, seconds=301)

    # Act and assert
    assert "speed_demon" not in _earned(db, slow, exam)

    fast = _graded(db, exam.id, student_id=102, score=90, seconds=299)

    # Act and assert
    assert "speed_demon" in _earned(db, fast, exam)


def test_given_exam_without_time_limit_when_speed_demon_checked_then_one_hour_is_used(
    db, seeded_badges
):
    # Arrange
    exam = _exam(db, time_limit_minutes=None)

    sub = _graded(db, exam.id, score=90, seconds=1199)

    # Act and assert
    assert "speed_demon" in _earned(db, sub, exam)


def test_given_improved_score_on_retake_when_badges_checked_then_improved_star_is_awarded(
    db, seeded_badges
):
    # Arrange
    exam = _exam(db)
    now = datetime.now(timezone.utc)
    _graded(db, exam.id, score=50, when=now - timedelta(hours=1))

    worse = _graded(db, exam.id, score=40, when=now - timedelta(minutes=30))

    # Act and assert
    assert "improved_star" not in _earned(db, worse, exam)

    better = _graded(db, exam.id, score=60, when=now)

    # Act and assert
    assert "improved_star" in _earned(db, better, exam)


def test_given_perfect_score_when_badges_checked_then_perfect_100_is_awarded_only_on_the_first_attempt(
    db, seeded_badges
):
    # Arrange
    exam = _exam(db)
    now = datetime.now(timezone.utc)
    _graded(db, exam.id, score=50, when=now - timedelta(hours=1))

    retake = _graded(db, exam.id, score=100, when=now)

    # Act and assert
    assert "perfect_100" not in _earned(db, retake, exam)


def test_given_five_first_passes_in_one_lesson_when_badges_checked_then_subject_expert_is_awarded(
    db, seeded_badges
):
    # Arrange
    exams = [_exam(db, module_id=7) for _ in range(5)]
    now = datetime.now(timezone.utc)
    for i, exam in enumerate(exams[:4]):
        _graded(db, exam.id, score=90, when=now - timedelta(hours=10 - i))
    # A retake that passes does not replace a failed first attempt.
    _graded(db, exams[4].id, score=50, when=now - timedelta(hours=2))

    retake = _graded(db, exams[4].id, score=95, when=now - timedelta(hours=1))

    # Act and assert
    assert "subject_expert" not in _earned(db, retake, exams[4])

    sixth = _exam(db, module_id=7)

    sub = _graded(db, sixth.id, score=90, when=now)

    # Act and assert
    assert "subject_expert" in _earned(db, sub, sixth)


def test_given_first_paper_when_badges_checked_then_first_step_is_awarded_only_once(
    db, seeded_badges
):
    # Arrange
    exam = _exam(db)
    now = datetime.now(timezone.utc)
    _graded(db, exam.id, score=10, when=now - timedelta(hours=1))

    second = _graded(db, _exam(db).id, score=90, when=now)

    # Act and assert
    assert "first_step" not in _earned(db, second, exam)


def test_given_one_hundred_attempts_when_badges_checked_then_century_club_is_awarded(
    db, seeded_badges
):
    # Arrange
    exam = _exam(db)
    start = datetime.now(timezone.utc) - timedelta(days=200)
    db.add_all(
        Submission(
            assessment_id=exam.id,
            student_id=101,
            status=SubmissionStatus.graded,
            score=10,
            started_at=start + timedelta(hours=i),
            submitted_at=start + timedelta(hours=i, minutes=5),
        )
        for i in range(98)
    )
    db.commit()

    sub = _graded(db, exam.id, score=10)

    # Act and assert
    assert "century_club" not in _earned(db, sub, exam)  # 99 papers

    sub = _graded(db, exam.id, score=10)

    # Act and assert
    assert "century_club" in _earned(db, sub, exam)


def _four_weeks(db, exam, student_id=101):
    now = datetime.now(timezone.utc)
    for weeks in (3, 2, 1):
        _graded(
            db,
            exam.id,
            student_id=student_id,
            score=10,
            when=now - timedelta(weeks=weeks),
        )
    return _graded(db, exam.id, student_id=student_id, score=10, when=now)


def test_given_four_active_weeks_and_an_old_account_when_badges_checked_then_junior_scholar_is_awarded(
    db, seeded_badges
):
    # Arrange
    exam = _exam(db)
    now = datetime.now(timezone.utc)
    _student(db, 101, account_created_at=now - timedelta(days=30))

    # Act and assert
    assert "junior_scholar" in _earned(db, _four_weeks(db, exam, 101), exam)

    _student(db, 102, account_created_at=now - timedelta(days=20))

    # Act and assert
    assert "junior_scholar" not in _earned(db, _four_weeks(db, exam, 102), exam)

    _student(db, 103)  # account age unknown

    # Act and assert
    assert "junior_scholar" not in _earned(db, _four_weeks(db, exam, 103), exam)


def test_given_earned_badge_when_awarded_twice_then_it_is_stored_once_and_announced(
    db, seeded_badges, sent
):
    # Arrange
    exam = _exam(db)

    first = _graded(db, exam.id, score=100, seconds=30)

    # Act and assert
    assert "perfect_100" in _earned(db, first, exam)
    assert ("perfect_100") in {p["badge_key"] for k, p in sent if k == ev.BADGE_AWARDED}

    again = _graded(db, _exam(db).id, score=100, seconds=30)

    # Act and assert
    assert "perfect_100" not in _earned(db, again, exam)


def test_given_concurrent_award_when_badge_awarded_then_the_duplicate_is_skipped(
    db, seeded_badges, sent
):
    # Arrange
    exam = _exam(db)

    sub = _graded(db, exam.id, score=10)
    trophy = db.query(Badge).filter_by(criteria_key="perfect_100").one()
    db.add(UserBadge(student_id=101, badge_id=trophy.id))  # written by another request
    db.commit()

    # Act and assert
    assert badges._award(db, 101, "perfect_100", owned=set()) is None
    assert sent == []
    assert badges._award(db, 101, "no_such_badge", owned=set()) is None
    assert sub.id  # the session is still usable after the rollback


def test_given_utc_time_when_converted_to_local_time_then_sri_lanka_time_is_returned():
    # Act and assert
    assert badges._local(datetime(2026, 1, 1, 0, 0)).hour == 5  # naive is UTC

    aware = datetime(2026, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=1)))

    # Act and assert
    assert badges._local(aware).hour == 4  # 23:00 UTC + 05:30


# ---------------------------------------------------------------------------
# resync
# ---------------------------------------------------------------------------


def test_given_existing_data_when_resync_runs_then_exams_badges_and_graded_attempts_are_announced(
    db, seeded_badges, sent
):
    # Act
    exam = _exam(db, module_id=7, pass_score=60)
    q1 = _question(db, exam.id, answer="A")
    _question(db, exam.id, answer="B", position=2)
    trophy = db.query(Badge).filter_by(criteria_key="perfect_100").one()
    db.add(UserBadge(student_id=101, badge_id=trophy.id))
    db.add(
        Submission(
            assessment_id=exam.id,
            student_id=101,
            status=SubmissionStatus.in_progress,
        )
    )
    db.commit()
    graded = _graded(
        db,
        exam.id,
        score=50,
        seconds=75,
        answers_json=json.dumps([{"question_id": q1.id, "selected_option": "A"}]),
    )
    _graded(db, 999)  # its exam was deleted

    resync.announce_everything()

    by_key = {}
    for key, payload in sent:
        by_key.setdefault(key, []).append(payload)

    # Assert
    assert [p["id"] for p in by_key[ev.ASSESSMENT_UPSERTED]] == [exam.id]
    assert by_key[ev.BADGE_AWARDED][0]["badge_key"] == "perfect_100"
    (result,) = by_key[ev.ASSESSMENT_GRADED]

    # Act and assert
    assert result["submission_id"] == graded.id
    assert (result["marks_obtained"], result["total_questions"]) == (1, 2)
    assert result["passed"] is False and result["module_id"] == 7
    assert result["time_seconds"] == 75


def test_given_empty_database_when_resync_runs_then_nothing_is_announced(sent):
    # Act
    resync.announce_everything()

    # Assert
    assert sent == []


# ---------------------------------------------------------------------------
# events, catalog, people
# ---------------------------------------------------------------------------


def test_given_assessment_when_event_payload_built_then_it_carries_the_assessment_fields(
    db,
):
    # Arrange
    exam = _exam(db, position=None)
    exam.created_at = None

    # Act
    payload = events.assessment_event(exam)

    # Assert
    assert payload["position"] == 0 and payload["created_at"] is None
    assert payload["is_published"] is True


def test_given_badge_and_graded_events_without_timestamps_when_built_then_timestamps_default(
    sent,
):
    # Arrange
    events.announce_badge(101, "first_step", None)

    # Act
    events.publish_assessment_graded(101, 1, 50.0, False)

    # Assert
    assert sent[0] == (
        ev.BADGE_AWARDED,
        {"student_id": 101, "badge_key": "first_step", "awarded_at": None},
    )
    assert sent[1][1]["submitted_at"] is None and sent[1][1]["course_id"] is None


def test_given_no_ids_when_lookups_run_then_the_database_is_not_queried(monkeypatch):
    # Arrange
    def no_db():
        raise AssertionError("should not open a session")

    monkeypatch.setattr(catalog, "SessionLocal", no_db)
    monkeypatch.setattr(people, "SessionLocal", no_db)

    # Act and assert
    assert run(catalog.get_course_titles([])) == {}
    assert run(people.get_profiles([])) == {}


def test_given_unknown_course_when_instructor_looked_up_then_none_is_returned():
    # Act and assert
    assert run(catalog.get_course_instructor_id(404)) is None


# ---------------------------------------------------------------------------
# In-memory Redis fallback
# ---------------------------------------------------------------------------


def test_given_in_memory_redis_when_value_set_read_and_deleted_then_it_behaves_like_redis():
    # Arrange
    r = MemoryRedisClient()

    # Act and assert
    assert r.get("k") is None
    r.set("k", 5)

    # Act and assert
    assert r.get("k") == "5"
    r.delete("k")
    r.delete("k")  # deleting twice is fine

    # Act and assert
    assert r.get("k") is None


def test_given_in_memory_redis_when_ttl_requested_then_remaining_time_is_reported():
    # Arrange
    r = MemoryRedisClient()

    # Act and assert
    assert r.ttl("missing") == -2
    r.set("forever", "v")

    # Act and assert
    assert r.ttl("forever") == -1
    r.set("timed", "v", ex=60)

    # Act and assert
    assert 58 <= r.ttl("timed") <= 60


def _expire(r: MemoryRedisClient, key: str):
    value, _ = r._store[key]
    r._store[key] = (value, datetime.now(timezone.utc) - timedelta(seconds=1))


def test_given_in_memory_redis_when_value_expires_then_it_is_no_longer_returned():
    # Arrange
    r = MemoryRedisClient()
    for key in ("a", "b", "c"):
        r.set(key, "v", ex=60)
        _expire(r, key)

    # Act and assert
    assert r.get("a") is None

    # Act and assert
    assert r.ttl("b") == -2
    assert r.keys("*") == []
    assert r._store == {}


def test_given_in_memory_redis_when_keys_listed_by_prefix_then_matching_keys_are_returned():
    # Act
    r = MemoryRedisClient()
    r.set("session:active:1", "x")
    r.set("session:active:2", "y", ex=60)
    r.set("other", "z")

    # Assert
    assert sorted(r.keys("session:active:*")) == [
        "session:active:1",
        "session:active:2",
    ]


def test_given_module_client_when_redis_client_requested_then_the_same_client_is_returned():
    # Act and assert
    assert redis_client.get_redis_client() is redis_client.redis_client


# ---------------------------------------------------------------------------
# router helpers on malformed data
# ---------------------------------------------------------------------------


def test_given_bad_json_when_answers_read_then_empty_answers_are_returned():
    # Act and assert
    assert router._answers_of(Submission(answers_json="not json")) == {}
    assert router._answers_of(Submission(answers_json='[{"no_id": 1}]')) == {}
    assert router._answers_of(Submission(answers_json=None)) == {}

    sub = Submission(answers_json='[{"question_id": "3", "selected_option": null}]')

    # Act and assert
    assert router._answers_of(sub) == {3: ""}


def test_given_edge_case_timestamps_when_time_taken_computed_then_expected_values_are_returned():
    # Arrange
    now = datetime.now(timezone.utc)

    # Act and assert
    assert router._time_taken(Submission(started_at=None, submitted_at=now)) == 0

    backwards = Submission(started_at=now, submitted_at=now - timedelta(seconds=5))

    # Act and assert
    assert router._time_taken(backwards) == 0


def test_given_bad_json_when_question_helpers_run_then_they_do_not_fail():
    # Arrange
    q = Question(
        id=1,
        assessment_id=1,
        question_text="Q",
        question_type=QuestionType.mcq,
        options_json="{broken",
        option_images_json="{broken",
        correct_answer="A",
        marks=1,
        position=1,
    )

    # Act and assert
    assert router._option_images(q, 2) == [None, None]
    assert router._question_to_admin_response(q).options == []

    # Act
    (item,) = router._build_review([q], {1: "A"})

    # Assert
    assert item.options == [] and item.is_correct is True


def test_given_option_image_lists_of_any_length_when_normalised_then_they_are_padded_or_trimmed():
    # Arrange
    q = Question(
        option_images_json=json.dumps([{"url": "a"}, {"url": "b"}, {"url": "c"}])
    )

    # Act and assert
    assert router._option_images(q, 2) == [{"url": "a"}, {"url": "b"}]
    assert router._option_images(q, 4)[3] is None


def test_given_naive_and_aware_times_when_converted_to_utc_then_both_are_aware():
    # Arrange
    naive = datetime(2026, 1, 1, 9)

    # Act and assert
    assert router._as_utc(naive).tzinfo == timezone.utc

    aware = datetime(2026, 1, 1, 9, tzinfo=timezone(timedelta(hours=5)))

    # Act and assert
    assert router._as_utc(aware) is aware
    assert router._as_utc(None) is None
