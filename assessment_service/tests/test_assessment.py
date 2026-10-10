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

    mock_publish.assert_called_once_with(
        student_id=103, assessment_id=1, score=100.0, passed=True
    )


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

    mock_publish.assert_called_once_with(
        student_id=104, assessment_id=1, score=50.0, passed=True
    )


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
    res = client.get("/questions/csv-template", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert res.text.splitlines()[0] == (
        "question_text,option_a,option_b,option_c,option_d,option_e,correct_answer,explanation"
    )


def test_csv_template_requires_teacher_or_admin():
    token = create_token(user_id=110, role="student")
    res = client.get("/questions/csv-template", headers={"Authorization": f"Bearer {token}"})
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
    imported = db.query(Question).filter(Question.question_text == "Capital of France?").one()
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
            id=2, course_id=10, module_id=7, position=1, title="Warm-up",
            assessment_type=AssessmentType.exam, is_published=True,
        )
    )
    db.commit()
    db.close()

    token = create_token(user_id=109, role="student")
    response = client.get(
        "/?module_id=7", headers={"Authorization": f"Bearer {token}"}
    )
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
    assert {e["badge"]["criteria_key"] for e in dashboard["earned_badges"]} >= first_keys
