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


def test_given_existing_assessment_when_fetched_then_assessment_returned():
    # Arrange
    token = create_token(user_id=101, role="student")

    # Act
    res = client.get(
        "/api/assessments/1",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Assert
    assert res.status_code == 200

    # Act
    data = res.json()

    # Assert
    assert data["id"] == 1
    assert data["title"] == "Python Basics Quiz"


def test_given_unknown_assessment_when_fetched_then_404_assessment_not_found():
    # Arrange
    token = create_token(user_id=101, role="student")

    # Act
    res = client.get(
        "/api/assessments/999",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Assert
    assert res.status_code == 404
    assert res.json()["detail"]["error"] == "ASSESSMENT_NOT_FOUND"


def test_given_enrolled_student_when_starting_assessment_then_session_is_created():
    # Arrange
    token = create_token(user_id=101, role="student")

    # Act
    response = client.post(
        "/api/assessments/1/start",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Assert
    assert response.status_code == 200

    # Act
    data = response.json()

    # Assert
    assert "session_id" in data
    assert data["assessment_id"] == 1
    assert data["time_remaining_seconds"] == 15 * 60
    assert len(data["questions"]) == 2
    for q in data["questions"]:
        assert "correct_answer" not in q


def test_given_active_session_when_starting_again_then_409_session_already_active():
    # Arrange
    token = create_token(user_id=102, role="student")

    # Act
    res1 = client.post(
        "/api/assessments/1/start",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Assert
    assert res1.status_code == 200
    session_id = res1.json()["session_id"]

    # Act
    res2 = client.post(
        "/api/assessments/1/start",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Assert
    assert res2.status_code == 409

    # Act
    err_data = res2.json()["detail"]

    # Assert
    assert err_data["error"] == "SESSION_ALREADY_ACTIVE"
    assert err_data["session_id"] == session_id


@patch("router.publish_assessment_graded")
def test_given_started_session_when_assessment_submitted_then_it_is_graded(
    mock_publish,
):
    # Arrange
    token = create_token(user_id=103, role="student")

    # Act
    start_res = client.post(
        "/api/assessments/1/start",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Assert
    assert start_res.status_code == 200
    session_id = start_res.json()["session_id"]

    submit_payload = {
        "session_id": session_id,
        "answers": [
            {"question_id": 1, "selected_option": "def"},
            {"question_id": 2, "selected_option": "False"},
        ],
    }

    # Act
    submit_res = client.post(
        "/api/assessments/1/submit",
        json=submit_payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    # Assert
    assert submit_res.status_code == 200

    # Act
    res_data = submit_res.json()

    # Assert
    assert res_data["score"] == 100.0
    assert res_data["passed"] is True
    assert res_data["correct_count"] == 2
    assert res_data["total_questions"] == 2
    assert res_data["auto_submitted"] is False

    mock_publish.assert_called_once()
    sent = mock_publish.call_args.kwargs

    # Act and assert
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
def test_given_expired_session_when_submitted_then_assessment_is_auto_submitted(
    mock_publish,
):
    # Arrange
    token = create_token(user_id=104, role="student")

    expired_session_id = "sess_expired_12345"
    submit_payload = {
        "session_id": expired_session_id,
        "answers": [
            {"question_id": 1, "selected_option": "def"},
            {"question_id": 2, "selected_option": "True"},
        ],
    }

    # Act
    submit_res = client.post(
        "/api/assessments/1/submit",
        json=submit_payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    # Assert
    assert submit_res.status_code == 200

    # Act
    res_data = submit_res.json()

    # Assert
    assert res_data["score"] == 50.0
    assert res_data["passed"] is True
    assert res_data["correct_count"] == 1
    assert res_data["auto_submitted"] is True

    mock_publish.assert_called_once()
    sent = mock_publish.call_args.kwargs

    # Act and assert
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


def test_given_active_session_when_tab_switch_reported_then_violation_is_recorded():
    # Arrange
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

    # Act
    v_res = client.post(
        "/api/assessments/1/violations",
        json=violation_payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    # Assert
    assert v_res.status_code == 204

    db = TestingSessionLocal()

    # Act
    v_log = db.query(ViolationLog).filter(ViolationLog.student_id == 105).first()

    # Assert
    assert v_log is not None
    assert v_log.violation_type == ViolationType.tab_switch
    db.close()


def test_given_other_violation_types_when_reported_then_204():
    # Arrange
    token = create_token(user_id=106, role="student")

    # Act
    v_res = client.post(
        "/api/assessments/1/violations",
        json={"session_id": "sess_dummy", "violation_type": "copy_paste"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Assert
    assert v_res.status_code == 204


def test_given_no_token_when_starting_assessment_then_401():
    # Act
    response = client.post("/api/assessments/1/start")

    # Assert
    assert response.status_code == 401


def test_given_student_not_enrolled_when_starting_assessment_then_403_not_enrolled():
    # Arrange
    token = create_token(user_id=107, role="STUDENT")

    # Act
    with patch("router.is_enrolled", new=AsyncMock(return_value=False)):
        response = client.post(
            "/api/assessments/1/start",
            headers={"Authorization": f"Bearer {token}"},
        )

    # Assert
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "NOT_ENROLLED"


def test_given_future_scheduled_start_when_starting_assessment_then_403():
    # Arrange
    db = TestingSessionLocal()
    db.query(Assessment).filter(Assessment.id == 1).update(
        {Assessment.scheduled_start: datetime.now(timezone.utc) + timedelta(hours=2)}
    )
    db.commit()
    db.close()

    token = create_token(user_id=108, role="student")

    # Act
    response = client.post(
        "/api/assessments/1/start",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Assert
    assert response.status_code == 403
    assert response.json()["detail"]["not_started_yet"] is True


def _import_csv(content: str, filename: str = "questions.csv"):
    token = create_token(user_id=1, role="ADMIN")
    return client.post(
        "/1/questions/import-csv",
        files={"file": (filename, content.encode("utf-8-sig"), "text/csv")},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_given_teacher_when_csv_template_requested_then_template_returned():
    # Arrange
    token = create_token(user_id=1, role="admin")

    # Act
    res = client.get(
        "/questions/csv-template", headers={"Authorization": f"Bearer {token}"}
    )

    # Assert
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert res.text.splitlines()[0] == (
        "question_text,option_a,option_b,option_c,option_d,option_e,correct_answer,explanation"
    )


def test_given_student_when_csv_template_requested_then_403():
    # Arrange
    token = create_token(user_id=110, role="student")

    # Act
    res = client.get(
        "/questions/csv-template", headers={"Authorization": f"Bearer {token}"}
    )

    # Assert
    assert res.status_code == 403


def test_given_csv_with_valid_and_bad_rows_when_imported_then_valid_rows_created_and_bad_ones_reported():
    # Arrange
    csv_text = (
        "question_text,option_a,option_b,option_c,option_d,option_e,correct_answer,explanation\n"
        "Capital of France?,London,Paris,Berlin,,,b,Paris is the capital.\n"
        "Only one option,Solo,,,,,A,\n"
        "Answer is an empty option,Yes,No,,,,D,\n"
        "Duplicate options,Same,Same,,,,A,\n"
    )

    # Act
    res = _import_csv(csv_text)

    # Assert
    assert res.status_code == 201

    # Act
    data = res.json()

    # Assert
    assert data["questions_created"] == 1
    assert len(data["errors"]) == 3
    assert data["errors"][0].startswith("Row 3:")

    db = TestingSessionLocal()

    imported = (
        db.query(Question).filter(Question.question_text == "Capital of France?").one()
    )

    # Act and assert
    assert imported.correct_answer == "Paris"
    assert imported.explanation == "Paris is the capital."
    assert json.loads(imported.options_json) == ["London", "Paris", "Berlin"]
    assert imported.position == 3  # appended after the two seeded questions
    db.close()


def test_given_non_csv_file_when_imported_then_415():
    # Act
    res = _import_csv("not,a,csv", filename="questions.txt")

    # Assert
    assert res.status_code == 415


def test_given_module_assessments_when_listed_then_ordered_by_position_with_question_counts():
    # Arrange
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

    # Act
    response = client.get("/?module_id=7", headers={"Authorization": f"Bearer {token}"})

    # Assert
    assert response.status_code == 200

    # Act
    data = response.json()

    # Assert
    assert [a["id"] for a in data] == [2, 1]
    assert [a["question_count"] for a in data] == [0, 2]


def test_given_perfect_first_attempt_when_submitted_then_badges_are_earned_once():
    # Arrange
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

    # Act
    with patch("router.publish_assessment_graded"):
        first = take_exam()
        second = take_exam()

    first_keys = {b["criteria_key"] for b in first["new_badges"]}

    # Assert
    assert {"first_step", "perfect_100", "speed_demon"} <= first_keys
    # Already-earned badges are never awarded twice.
    assert not ({b["criteria_key"] for b in second["new_badges"]} & first_keys)

    # Act
    dashboard = client.get("/badges/dashboard", headers=headers).json()

    # Assert
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


def test_given_tied_scores_and_districts_when_ranked_then_ties_share_a_rank_and_districts_rank_separately():
    # Act
    rows = rank_rows(
        [
            {"id": "a", "score": 90, "time_seconds": 100, "district": "Colombo"},
            {"id": "b", "score": 90, "time_seconds": 100, "district": "Kandy"},
            {"id": "c", "score": 70, "time_seconds": 50, "district": "Colombo"},
            {"id": "d", "score": 90, "time_seconds": 120, "district": "Colombo"},
        ]
    )

    # Assert
    assert [(r["id"], r["rank"], r["district_rank"]) for r in rows] == [
        ("a", 1, 1),
        ("b", 1, 1),
        ("d", 3, 2),
        ("c", 4, 3),
    ]


def test_given_no_token_when_leaderboard_requested_then_ranking_uses_first_attempts(
    ranked_attempts,
):
    # Act
    r = client.get("/rankings/leaderboard", params={"exam_id": 1})

    # Assert
    assert r.status_code == 200

    # Act
    board = r.json()

    # Assert
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


def test_given_signed_in_student_when_leaderboard_requested_then_caller_is_marked_and_district_filter_applies(
    ranked_attempts,
):
    # Arrange
    headers = {"Authorization": f"Bearer {create_token(203)}"}

    # Act
    board = client.get(
        "/rankings/leaderboard",
        params={"exam_id": 1, "district": "colombo"},
        headers=headers,
    ).json()

    # Assert
    assert [(row["full_name"], row["is_current_user"]) for row in board] == [
        ("Ama Perera", False),
        ("Nimal Fernando", True),
    ]

    # Act and assert
    assert (
        len(
            client.get(
                "/rankings/leaderboard", params={"exam_id": 1, "limit": 1, "offset": 1}
            ).json()
        )
        == 1
    )


def test_given_unpublished_exam_when_leaderboard_requested_then_it_is_hidden(
    ranked_attempts,
):
    # Arrange
    db = TestingSessionLocal()
    db.query(Assessment).filter(Assessment.id == 1).update({"is_published": False})
    db.commit()
    db.close()

    # Act and assert
    assert client.get("/rankings/leaderboard", params={"exam_id": 1}).status_code == 404
    assert client.get("/rankings/exams").json() == []


def test_given_attempts_when_ranking_exams_requested_then_public_and_per_student_views_differ(
    ranked_attempts,
):
    # Act and assert
    assert client.get("/rankings/exams").json() == [
        {
            "exam_id": "1",
            "exam_title": "Python Basics Quiz",
            "course_title": "Python",
            "subject": "Python",
        }
    ]
    stranger = {"Authorization": f"Bearer {create_token(555)}"}

    # Act and assert
    assert client.get("/rankings/exams", headers=stranger).json() == []


def test_given_no_token_when_exam_rank_requested_then_401_and_signed_in_student_gets_rank(
    ranked_attempts,
):
    # Act
    mine = client.get(
        "/rankings/exam/1", headers={"Authorization": f"Bearer {create_token(201)}"}
    ).json()

    # Assert
    assert (mine["overall_rank"], mine["district_rank"], mine["course_title"]) == (
        2,
        1,
        "Python",
    )

    # Act
    stranger = client.get(
        "/rankings/exam/1", headers={"Authorization": f"Bearer {create_token(555)}"}
    ).json()

    # Assert
    assert stranger["overall_rank"] is None

    # Act and assert
    assert client.get("/rankings/exam/1").status_code == 401


def test_given_retakes_when_result_rank_requested_then_it_matches_the_leaderboard_and_ignores_retakes(
    ranked_attempts,
):
    # 203 first scored 60 (3rd); a perfect retake now must not lift that rank.
    # Arrange
    headers = {"Authorization": f"Bearer {create_token(203)}"}

    # Act
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

    # Assert
    assert result["score"] == 100
    assert (result["ranking"]["overall_rank"], result["ranking"]["district_rank"]) == (
        3,
        2,
    )

    # Act
    attempts = client.get("/my-submissions", headers=headers).json()

    # Assert
    assert {(a["overall_rank"], a["district_rank"]) for a in attempts} == {(3, 2)}

    # Act
    last = client.get("/1/my-last-attempt", headers=headers).json()

    # Assert
    assert last["ranking"]["overall_rank"] == 3

    # Act
    review = client.get(
        f"/submissions/{attempts[0]['attempt_id']}/review", headers=headers
    ).json()

    # Assert
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


def test_given_student_missing_from_access_list_when_starting_exam_then_403_not_enrolled(
    local_copies,
):
    # Arrange
    deliver, _ = local_copies
    headers = {"Authorization": f"Bearer {create_token(user_id=300, role='student')}"}

    # Act
    denied = client.post("/api/assessments/1/start", headers=headers)

    # Assert
    assert (
        denied.status_code == 403 and denied.json()["detail"]["error"] == "NOT_ENROLLED"
    )

    deliver(
        "access.changed",
        {"student_id": 300, "courses": [{"id": 10, "until": None}], "lessons": []},
    )

    # Act and assert
    assert client.post("/api/assessments/1/start", headers=headers).status_code == 200


def test_given_lesson_grant_or_expired_grant_when_access_checked_then_grant_rules_are_applied(
    local_copies,
):
    # Arrange
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

    # Act and assert
    assert asyncio.run(access_module.is_enrolled(301, 10, 55)) is True
    assert asyncio.run(access_module.is_enrolled(301, 10, 56)) is False
    assert asyncio.run(access_module.is_enrolled(302, 10)) is False


def test_given_teacher_when_managing_exams_then_only_exams_of_owned_courses_are_allowed(
    local_copies,
):
    # Arrange
    deliver, sent = local_copies
    deliver("course.upserted", _course_event(instructor_id=5))
    body = {"course_id": 10, "title": "Unit test", "is_published": True}

    owner = {"Authorization": f"Bearer {create_token(user_id=5, role='teacher')}"}
    other = {"Authorization": f"Bearer {create_token(user_id=6, role='teacher')}"}
    admin = {"Authorization": f"Bearer {create_token(user_id=1, role='admin')}"}

    # Act and assert
    assert client.post("/", json=body, headers=other).status_code == 403
    assert (
        client.post("/", json={**body, "course_id": 999}, headers=other).status_code
        == 404
    )

    # Act
    created = client.post("/", json=body, headers=owner)

    # Assert
    assert created.status_code == 201

    # Act and assert
    assert client.post("/", json=body, headers=admin).status_code == 201

    # every change to an exam is announced for the services that keep a copy
    key, payload = sent[0]

    # Act and assert
    assert (
        key == "assessment.upserted"
        and payload["course_id"] == 10
        and payload["is_published"] is True
    )

    exam_id = created.json()["id"]

    # Act and assert
    assert (
        client.put(
            f"/{exam_id}", json={"is_published": False}, headers=owner
        ).status_code
        == 200
    )

    # Act and assert
    assert sent[-1][1]["is_published"] is False

    # Act and assert
    assert client.delete(f"/{exam_id}", headers=owner).status_code == 204

    # Act and assert
    assert sent[-1] == ("assessment.deleted", {"id": exam_id})


def test_given_local_copies_when_titles_and_names_requested_then_values_come_from_the_copies(
    local_copies,
):
    # Arrange
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

    # Act and assert
    assert asyncio.run(catalog.get_course_titles([10, 11])) == {10: "Python Basics"}
    assert asyncio.run(catalog.get_course_instructor_id(10)) == 5

    # Act
    profiles = asyncio.run(people.get_profiles([400, 401]))

    # Assert
    assert profiles[400]["full_name"] == "Ama Perera" and 401 not in profiles


def test_given_graded_attempt_when_submitted_then_graded_event_is_published_with_the_full_result(
    local_copies,
):
    # Arrange
    deliver, sent = local_copies
    deliver(
        "access.changed",
        {"student_id": 310, "courses": [{"id": 10, "until": None}], "lessons": []},
    )
    headers = {"Authorization": f"Bearer {create_token(user_id=310, role='student')}"}
    start = client.post("/api/assessments/1/start", headers=headers).json()

    # Act
    client.post(
        "/api/assessments/1/submit",
        json={
            "session_id": start["session_id"],
            "answers": [{"question_id": 1, "selected_option": "def"}],
        },
        headers=headers,
    )
    graded = [p for k, p in sent if k == "assessment.graded"]

    # Assert
    assert len(graded) == 1
    assert graded[0]["student_id"] == 310 and graded[0]["course_id"] == 10
    assert graded[0]["total_questions"] == 2 and graded[0]["submission_id"]
