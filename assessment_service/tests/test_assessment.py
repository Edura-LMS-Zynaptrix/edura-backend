import asyncio
import json
import os
import sys

# Ensure assessment_service root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from database import Base, get_db
from fastapi.testclient import TestClient
from main import app
from models import (
    Assessment,
    AssessmentType,
    Question,
    QuestionType,
    ReplicaAccess,
    ReplicaCourse,
    ReplicaUser,
    Submission,
    ViolationLog,
    ViolationType,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.auth import SECRET_KEY

# In-memory SQLite for fast, isolated unit testing
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


def create_token(user_id: int = 101, role: str = "student") -> str:
    payload = {"sub": str(user_id), "role": role}
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


@pytest.fixture(autouse=True)
def isolated_redis():
    # A fresh in-memory store per test: never touches the shared Redis, and
    # one test's active exam session can't leak into the next run.
    from redis_client import MemoryRedisClient

    with patch("router.get_redis_client", return_value=MemoryRedisClient()):
        yield


@pytest.fixture(autouse=True)
def students_by_default():
    # user_service is a separate service: every id is a student unless a test says otherwise.
    def profiles(ids):
        return {
            i: {
                "user_id": i,
                "role": "student",
                "full_name": f"Student {i}",
                "district": "",
            }
            for i in ids
        }

    with patch("router.get_profiles", new=AsyncMock(side_effect=profiles)):
        yield


@pytest.fixture(autouse=True)
def enrolled_by_default():
    # enrollment_service is a separate service; individual tests override this.
    with patch("router.is_enrolled", new=AsyncMock(return_value=True)):
        yield


@pytest.fixture(autouse=True)
def setup_db():
    db = TestingSessionLocal()

    # Clear existing data between tests
    db.query(ViolationLog).delete()
    db.query(Submission).delete()
    db.query(Question).delete()
    db.query(Assessment).delete()
    db.commit()

    # Seed assessment
    assessment = Assessment(
        id=1,
        course_id=10,
        lesson_id=5,
        title="Python Basics Quiz",
        description="Test basic Python concepts",
        assessment_type=AssessmentType.quiz,
        time_limit_minutes=15,
        max_score=100,
        pass_score=50,
        is_published=True,
    )
    db.add(assessment)
    db.commit()

    # Seed questions
    q1 = Question(
        id=1,
        assessment_id=1,
        question_text="What is the keyword to define a function in Python?",
        question_type=QuestionType.mcq,
        options_json=json.dumps(["def", "func", "function", "define"]),
        correct_answer="def",
        marks=50,
        position=1,
    )
    q2 = Question(
        id=2,
        assessment_id=1,
        question_text="Python is a compiled language. True or False?",
        question_type=QuestionType.true_false,
        options_json=json.dumps(["True", "False"]),
        correct_answer="False",
        marks=50,
        position=2,
    )
    db.add_all([q1, q2])
    db.commit()
    db.close()

    yield


client = TestClient(app)


def test_get_assessment():
    token = create_token(user_id=101, role="student")
    res = client.get(
        "/api/assessments/1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == 1
    assert data["title"] == "Python Basics Quiz"


def test_get_assessment_not_found():
    token = create_token(user_id=101, role="student")
    res = client.get(
        "/api/assessments/999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 404
    assert res.json()["detail"]["error"] == "ASSESSMENT_NOT_FOUND"


def test_start_session_ac1():
    token = create_token(user_id=101, role="student")
    response = client.post(
        "/api/assessments/1/start",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "session_id" in data
    assert data["assessment_id"] == 1
    assert data["time_remaining_seconds"] == 15 * 60
    assert len(data["questions"]) == 2
    for q in data["questions"]:
        assert "correct_answer" not in q


def test_start_session_duplicate_blocked_ac5():
    token = create_token(user_id=102, role="student")

    res1 = client.post(
        "/api/assessments/1/start",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res1.status_code == 200
    session_id = res1.json()["session_id"]

    res2 = client.post(
        "/api/assessments/1/start",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res2.status_code == 409
    err_data = res2.json()["detail"]
    assert err_data["error"] == "SESSION_ALREADY_ACTIVE"
    assert err_data["session_id"] == session_id


@patch("router.publish_assessment_graded")
def test_submit_assessment_ac2(mock_publish):
    token = create_token(user_id=103, role="student")

    start_res = client.post(
        "/api/assessments/1/start",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert start_res.status_code == 200
    session_id = start_res.json()["session_id"]

    submit_payload = {
        "session_id": session_id,
        "answers": [
            {"question_id": 1, "selected_option": "def"},
            {"question_id": 2, "selected_option": "False"},
        ],
    }

    submit_res = client.post(
        "/api/assessments/1/submit",
        json=submit_payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert submit_res.status_code == 200
    res_data = submit_res.json()
    assert res_data["score"] == 100.0
    assert res_data["passed"] is True
    assert res_data["correct_count"] == 2
    assert res_data["total_questions"] == 2
    assert res_data["auto_submitted"] is False

    mock_publish.assert_called_once()
    sent = mock_publish.call_args.kwargs
    assert (
        sent["student_id"],
        sent["assessment_id"],
        sent["score"],
        sent["passed"],
    ) == (103, 1, 100.0, True)
    # the event carries the whole result, so listeners (progress, analytics) need nothing else
    assert sent["course_id"] == 10 and sent["submission_id"] is not None
    assert sent["total_questions"] == 2 and sent["marks_obtained"] is not None
    assert sent["submitted_at"] is not None


@patch("router.publish_assessment_graded")
def test_auto_submit_on_expiry_ac3(mock_publish):
    token = create_token(user_id=104, role="student")

    expired_session_id = "sess_expired_12345"
    submit_payload = {
        "session_id": expired_session_id,
        "answers": [
            {"question_id": 1, "selected_option": "def"},
            {"question_id": 2, "selected_option": "True"},
        ],
    }

    submit_res = client.post(
        "/api/assessments/1/submit",
        json=submit_payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert submit_res.status_code == 200
    res_data = submit_res.json()
    assert res_data["score"] == 50.0
    assert res_data["passed"] is True
    assert res_data["correct_count"] == 1
    assert res_data["auto_submitted"] is True

    mock_publish.assert_called_once()
    sent = mock_publish.call_args.kwargs
    assert (
        sent["student_id"],
        sent["assessment_id"],
        sent["score"],
        sent["passed"],
    ) == (104, 1, 50.0, True)
    # the event carries the whole result, so listeners (progress, analytics) need nothing else
    assert sent["course_id"] == 10 and sent["submission_id"] is not None
    assert sent["total_questions"] == 2 and sent["marks_obtained"] is not None
    assert sent["submitted_at"] is not None


def test_tab_switch_violation_ac4():
    token = create_token(user_id=105, role="student")

    start_res = client.post(
        "/api/assessments/1/start",
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = start_res.json()["session_id"]

    violation_payload = {
        "session_id": session_id,
        "violation_type": "TAB_SWITCH",
        "detail": "Student switched window tab",
        "timestamp": "2026-08-24T12:00:00Z",
    }
    v_res = client.post(
        "/api/assessments/1/violations",
        json=violation_payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert v_res.status_code == 204

    db = TestingSessionLocal()
    v_log = db.query(ViolationLog).filter(ViolationLog.student_id == 105).first()
    assert v_log is not None
    assert v_log.violation_type == ViolationType.tab_switch
    db.close()


def test_other_violation_types():
    token = create_token(user_id=106, role="student")
    v_res = client.post(
        "/api/assessments/1/violations",
        json={"session_id": "sess_dummy", "violation_type": "copy_paste"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert v_res.status_code == 204


def test_unauthorized_access():
    response = client.post("/api/assessments/1/start")
    assert response.status_code == 401


def test_start_requires_enrollment():
    token = create_token(user_id=107, role="STUDENT")
    with patch("router.is_enrolled", new=AsyncMock(return_value=False)):
        response = client.post(
            "/api/assessments/1/start",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "NOT_ENROLLED"


def test_start_blocked_before_scheduled_start():
    db = TestingSessionLocal()
    db.query(Assessment).filter(Assessment.id == 1).update(
        {Assessment.scheduled_start: datetime.now(timezone.utc) + timedelta(hours=2)}
    )
    db.commit()
    db.close()

    token = create_token(user_id=108, role="student")
    response = client.post(
        "/api/assessments/1/start",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["not_started_yet"] is True


def _import_csv(content: str, filename: str = "questions.csv"):
    token = create_token(user_id=1, role="ADMIN")
    return client.post(
        "/1/questions/import-csv",
        files={"file": (filename, content.encode("utf-8-sig"), "text/csv")},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_csv_template_download():
    token = create_token(user_id=1, role="admin")
    res = client.get(
        "/questions/csv-template", headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert res.text.splitlines()[0] == (
        "question_text,option_a,option_b,option_c,option_d,option_e,correct_answer,explanation"
    )


def test_csv_template_requires_teacher_or_admin():
    token = create_token(user_id=110, role="student")
    res = client.get(
        "/questions/csv-template", headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 403


def test_csv_import_creates_valid_rows_and_reports_bad_ones():
    csv_text = (
        "question_text,option_a,option_b,option_c,option_d,option_e,correct_answer,explanation\n"
        "Capital of France?,London,Paris,Berlin,,,b,Paris is the capital.\n"
        "Only one option,Solo,,,,,A,\n"
        "Answer is an empty option,Yes,No,,,,D,\n"
        "Duplicate options,Same,Same,,,,A,\n"
    )
    res = _import_csv(csv_text)
    assert res.status_code == 201
    data = res.json()
    assert data["questions_created"] == 1
    assert len(data["errors"]) == 3
    assert data["errors"][0].startswith("Row 3:")

    db = TestingSessionLocal()
    imported = (
        db.query(Question).filter(Question.question_text == "Capital of France?").one()
    )
    assert imported.correct_answer == "Paris"
    assert imported.explanation == "Paris is the capital."
    assert json.loads(imported.options_json) == ["London", "Paris", "Berlin"]
    assert imported.position == 3  # appended after the two seeded questions
    db.close()


def test_csv_import_rejects_non_csv():
    res = _import_csv("not,a,csv", filename="questions.txt")
    assert res.status_code == 415


def test_list_by_module_orders_by_position_and_counts_questions():
    db = TestingSessionLocal()
    db.query(Assessment).filter(Assessment.id == 1).update(
        {Assessment.module_id: 7, Assessment.position: 2}
    )
    db.add(
        Assessment(
            id=2,
            course_id=10,
            module_id=7,
            position=1,
            title="Warm-up",
            assessment_type=AssessmentType.exam,
            is_published=True,
        )
    )
    db.commit()
    db.close()

    token = create_token(user_id=109, role="student")
    response = client.get("/?module_id=7", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert [a["id"] for a in data] == [2, 1]
    assert [a["question_count"] for a in data] == [0, 2]


def test_perfect_first_attempt_earns_badges_once():
    from badges import seed_badges
    from models import UserBadge

    db = TestingSessionLocal()
    seed_badges(db)
    db.query(UserBadge).delete()
    db.commit()
    db.close()

    token = create_token(user_id=120, role="student")
    headers = {"Authorization": f"Bearer {token}"}

    def take_exam():
        start = client.post("/api/assessments/1/start", headers=headers).json()
        return client.post(
            "/api/assessments/1/submit",
            json={
                "session_id": start["session_id"],
                "answers": [
                    {"question_id": 1, "selected_option": "def"},
                    {"question_id": 2, "selected_option": "False"},
                ],
            },
            headers=headers,
        ).json()

    with patch("router.publish_assessment_graded"):
        first = take_exam()
        second = take_exam()

    first_keys = {b["criteria_key"] for b in first["new_badges"]}
    assert {"first_step", "perfect_100", "speed_demon"} <= first_keys
    # Already-earned badges are never awarded twice.
    assert not ({b["criteria_key"] for b in second["new_badges"]} & first_keys)

    dashboard = client.get("/badges/dashboard", headers=headers).json()
    assert len(dashboard["available_badges"]) == 10
    assert {
        e["badge"]["criteria_key"] for e in dashboard["earned_badges"]
    } >= first_keys


# ---------------------------------------------------------------------------
# Public rankings
# ---------------------------------------------------------------------------

from models import SubmissionStatus  # noqa: E402
from rankings import rank_rows  # noqa: E402

PROFILES = {
    201: {
        "user_id": 201,
        "role": "student",
        "full_name": "Ama Perera",
        "school": "Visakha",
        "district": "Colombo",
        "grade": 11,
    },
    202: {
        "user_id": 202,
        "role": "student",
        "full_name": "Kamal Silva",
        "school": "Trinity",
        "district": "Kandy",
        "grade": 11,
    },
    203: {
        "user_id": 203,
        "role": "student",
        "full_name": "Nimal Fernando",
        "school": "Royal",
        "district": "Colombo",
        "grade": 11,
    },
    900: {
        "user_id": 900,
        "role": "teacher",
        "full_name": "Staff Tester",
        "school": "",
        "district": "Colombo",
        "grade": None,
    },
}


def _graded(student_id, score, seconds, minutes_ago):
    submitted = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return Submission(
        assessment_id=1,
        student_id=student_id,
        status=SubmissionStatus.graded,
        score=score,
        started_at=submitted - timedelta(seconds=seconds),
        submitted_at=submitted,
    )


@pytest.fixture
def ranked_attempts():
    db = TestingSessionLocal()
    db.add_all(
        [
            _graded(201, 80, 300, minutes_ago=50),
            _graded(201, 100, 100, minutes_ago=10),  # retake: must not count
            _graded(202, 80, 200, minutes_ago=40),
            _graded(203, 60, 100, minutes_ago=30),
            _graded(900, 100, 50, minutes_ago=20),  # staff: kept off the board
        ]
    )
    db.commit()
    db.close()
    with (
        patch(
            "router.get_profiles",
            new=AsyncMock(
                side_effect=lambda ids: {i: PROFILES[i] for i in ids if i in PROFILES}
            ),
        ),
        patch("router.get_course_titles", new=AsyncMock(return_value={10: "Python"})),
    ):
        yield


def test_rank_rows_ties_share_a_rank_and_districts_rank_separately():
    rows = rank_rows(
        [
            {"id": "a", "score": 90, "time_seconds": 100, "district": "Colombo"},
            {"id": "b", "score": 90, "time_seconds": 100, "district": "Kandy"},
            {"id": "c", "score": 70, "time_seconds": 50, "district": "Colombo"},
            {"id": "d", "score": 90, "time_seconds": 120, "district": "Colombo"},
        ]
    )
    assert [(r["id"], r["rank"], r["district_rank"]) for r in rows] == [
        ("a", 1, 1),
        ("b", 1, 1),
        ("d", 3, 2),
        ("c", 4, 3),
    ]


def test_leaderboard_is_public_and_uses_first_attempts(ranked_attempts):
    r = client.get("/rankings/leaderboard", params={"exam_id": 1})
    assert r.status_code == 200
    board = r.json()
    assert [
        (row["full_name"], row["score"], row["rank"], row["district_rank"])
        for row in board
    ] == [
        ("Kamal Silva", 80, 1, 1),  # same score as Ama, faster
        ("Ama Perera", 80, 2, 1),  # her 100% retake doesn't count
        ("Nimal Fernando", 60, 3, 2),
    ]
    assert not any(row["is_current_user"] for row in board)
    assert "student_id" not in board[0] and "user_id" not in board[0]


def test_leaderboard_marks_caller_and_filters_district(ranked_attempts):
    headers = {"Authorization": f"Bearer {create_token(203)}"}
    board = client.get(
        "/rankings/leaderboard",
        params={"exam_id": 1, "district": "colombo"},
        headers=headers,
    ).json()
    assert [(row["full_name"], row["is_current_user"]) for row in board] == [
        ("Ama Perera", False),
        ("Nimal Fernando", True),
    ]
    assert (
        len(
            client.get(
                "/rankings/leaderboard", params={"exam_id": 1, "limit": 1, "offset": 1}
            ).json()
        )
        == 1
    )


def test_leaderboard_hides_unpublished_exams(ranked_attempts):
    db = TestingSessionLocal()
    db.query(Assessment).filter(Assessment.id == 1).update({"is_published": False})
    db.commit()
    db.close()
    assert client.get("/rankings/leaderboard", params={"exam_id": 1}).status_code == 404
    assert client.get("/rankings/exams").json() == []


def test_ranking_exams_public_and_per_student(ranked_attempts):
    assert client.get("/rankings/exams").json() == [
        {
            "exam_id": "1",
            "exam_title": "Python Basics Quiz",
            "course_title": "Python",
            "subject": "Python",
        }
    ]
    stranger = {"Authorization": f"Bearer {create_token(555)}"}
    assert client.get("/rankings/exams", headers=stranger).json() == []


def test_my_exam_rank(ranked_attempts):
    mine = client.get(
        "/rankings/exam/1", headers={"Authorization": f"Bearer {create_token(201)}"}
    ).json()
    assert (mine["overall_rank"], mine["district_rank"], mine["course_title"]) == (
        2,
        1,
        "Python",
    )
    stranger = client.get(
        "/rankings/exam/1", headers={"Authorization": f"Bearer {create_token(555)}"}
    ).json()
    assert stranger["overall_rank"] is None
    assert client.get("/rankings/exam/1").status_code == 401


def test_result_rank_matches_leaderboard_and_ignores_retakes(ranked_attempts):
    # 203 first scored 60 (3rd); a perfect retake now must not lift that rank.
    headers = {"Authorization": f"Bearer {create_token(203)}"}
    with patch("router.publish_assessment_graded"):
        start = client.post("/api/assessments/1/start", headers=headers).json()
        result = client.post(
            "/api/assessments/1/submit",
            json={
                "session_id": start["session_id"],
                "answers": [
                    {"question_id": 1, "selected_option": "def"},
                    {"question_id": 2, "selected_option": "False"},
                ],
            },
            headers=headers,
        ).json()
    assert result["score"] == 100
    assert (result["ranking"]["overall_rank"], result["ranking"]["district_rank"]) == (
        3,
        2,
    )

    attempts = client.get("/my-submissions", headers=headers).json()
    assert {(a["overall_rank"], a["district_rank"]) for a in attempts} == {(3, 2)}
    last = client.get("/1/my-last-attempt", headers=headers).json()
    assert last["ranking"]["overall_rank"] == 3
    review = client.get(
        f"/submissions/{attempts[0]['attempt_id']}/review", headers=headers
    ).json()
    assert (review["ranking"]["overall_rank"], review["ranking"]["district_rank"]) == (
        3,
        2,
    )


# ---------------------------------------------------------------------------
# No direct calls: access, owners, titles and names come from this service's copies
# ---------------------------------------------------------------------------

import access as access_module  # noqa: E402
import catalog  # noqa: E402
import event_consumer  # noqa: E402
import events as events_module  # noqa: E402
import people  # noqa: E402


@pytest.fixture()
def local_copies(monkeypatch):
    """Use the real local lookups (the default fixtures replace them with stubs)."""
    for module in (access_module, catalog, people, event_consumer):
        monkeypatch.setattr(module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("router.is_enrolled", access_module.is_enrolled)
    monkeypatch.setattr(
        "router.get_course_instructor_id", catalog.get_course_instructor_id
    )
    monkeypatch.setattr("router.get_course_titles", catalog.get_course_titles)
    monkeypatch.setattr("router.get_profiles", people.get_profiles)
    sent = []
    monkeypatch.setattr(
        events_module,
        "publish",
        lambda key, payload: sent.append((key, payload)) or True,
    )
    yield event_consumer.handle, sent
    db = TestingSessionLocal()
    for model in (ReplicaAccess, ReplicaCourse, ReplicaUser):
        db.query(model).delete()
    db.commit()
    db.close()


def _course_event(instructor_id=5, title="Python"):
    return {
        "id": 10,
        "title": title,
        "instructor_id": instructor_id,
        "status": "PUBLISHED",
    }


def test_sitting_an_exam_needs_the_access_list_to_say_so(local_copies):
    deliver, _ = local_copies
    headers = {"Authorization": f"Bearer {create_token(user_id=300, role='student')}"}

    denied = client.post("/api/assessments/1/start", headers=headers)
    assert (
        denied.status_code == 403 and denied.json()["detail"]["error"] == "NOT_ENROLLED"
    )

    deliver(
        "access.changed",
        {"student_id": 300, "courses": [{"id": 10, "until": None}], "lessons": []},
    )
    assert client.post("/api/assessments/1/start", headers=headers).status_code == 200


def test_a_lesson_grant_or_an_expired_grant_is_honoured(local_copies):
    deliver, _ = local_copies
    deliver("access.changed", {"student_id": 301, "courses": [], "lessons": [55]})
    deliver(
        "access.changed",
        {
            "student_id": 302,
            "courses": [{"id": 10, "until": "2020-01-01T00:00:00+00:00"}],
            "lessons": [],
        },
    )
    assert asyncio.run(access_module.is_enrolled(301, 10, 55)) is True
    assert asyncio.run(access_module.is_enrolled(301, 10, 56)) is False
    assert asyncio.run(access_module.is_enrolled(302, 10)) is False


def test_a_teacher_can_only_manage_exams_of_courses_they_own(local_copies):
    deliver, sent = local_copies
    deliver("course.upserted", _course_event(instructor_id=5))
    body = {"course_id": 10, "title": "Unit test", "is_published": True}

    owner = {"Authorization": f"Bearer {create_token(user_id=5, role='teacher')}"}
    other = {"Authorization": f"Bearer {create_token(user_id=6, role='teacher')}"}
    admin = {"Authorization": f"Bearer {create_token(user_id=1, role='admin')}"}
    assert client.post("/", json=body, headers=other).status_code == 403
    assert (
        client.post("/", json={**body, "course_id": 999}, headers=other).status_code
        == 404
    )

    created = client.post("/", json=body, headers=owner)
    assert created.status_code == 201
    assert client.post("/", json=body, headers=admin).status_code == 201

    # every change to an exam is announced for the services that keep a copy
    key, payload = sent[0]
    assert (
        key == "assessment.upserted"
        and payload["course_id"] == 10
        and payload["is_published"] is True
    )

    exam_id = created.json()["id"]
    assert (
        client.put(
            f"/{exam_id}", json={"is_published": False}, headers=owner
        ).status_code
        == 200
    )
    assert sent[-1][1]["is_published"] is False
    assert client.delete(f"/{exam_id}", headers=owner).status_code == 204
    assert sent[-1] == ("assessment.deleted", {"id": exam_id})


def test_titles_and_leaderboard_names_come_from_the_local_copies(local_copies):
    deliver, _ = local_copies
    deliver("course.upserted", _course_event(title="Python Basics"))
    deliver(
        "user.profile-upserted",
        {
            "user_id": 400,
            "role": "student",
            "full_name": "Ama Perera",
            "district": "Colombo",
            "grade": 11,
        },
    )
    assert asyncio.run(catalog.get_course_titles([10, 11])) == {10: "Python Basics"}
    assert asyncio.run(catalog.get_course_instructor_id(10)) == 5
    profiles = asyncio.run(people.get_profiles([400, 401]))
    assert profiles[400]["full_name"] == "Ama Perera" and 401 not in profiles


def test_the_graded_event_is_published_with_the_full_result(local_copies):
    deliver, sent = local_copies
    deliver(
        "access.changed",
        {"student_id": 310, "courses": [{"id": 10, "until": None}], "lessons": []},
    )
    headers = {"Authorization": f"Bearer {create_token(user_id=310, role='student')}"}
    start = client.post("/api/assessments/1/start", headers=headers).json()
    client.post(
        "/api/assessments/1/submit",
        json={
            "session_id": start["session_id"],
            "answers": [{"question_id": 1, "selected_option": "def"}],
        },
        headers=headers,
    )
    graded = [p for k, p in sent if k == "assessment.graded"]
    assert len(graded) == 1
    assert graded[0]["student_id"] == 310 and graded[0]["course_id"] == 10
    assert graded[0]["total_questions"] == 2 and graded[0]["submission_id"]
