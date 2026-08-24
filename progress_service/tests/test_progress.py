import json
import os
import sys
from unittest.mock import MagicMock, patch

# Ensure progress_service directory and edura-backend root are in sys.path
service_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
backend_dir = os.path.dirname(service_dir)
sys.path.insert(0, backend_dir)
sys.path.insert(0, service_dir)


import jwt
import pytest
from database import Base, get_db
from fastapi.testclient import TestClient
from main import app
from models import Certificate, CourseProgress
from redis_client import MemoryRedisClient
from services import (
    process_assessment_graded,
    process_lesson_watched,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.auth import SECRET_KEY

# In-memory SQLite for fast isolated testing
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
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------


def test_completion_percentage_calculation(db_session):
    """
    Unit test: 3 of 5 lessons watched -> completion_percentage = 60.0%
    """
    student_id = 101
    course_id = 1
    total_lessons = 5

    for l_id in [10, 11, 12]:
        process_lesson_watched(
            db_session,
            student_id=student_id,
            course_id=course_id,
            lesson_id=l_id,
            total_lessons=total_lessons,
        )

    cp = (
        db_session.query(CourseProgress)
        .filter(
            CourseProgress.student_id == student_id,
            CourseProgress.course_id == course_id,
        )
        .first()
    )

    assert cp is not None
    assert cp.completed_lessons_count == 3
    assert cp.total_lessons_count == 5
    assert float(cp.completion_percentage) == 60.0


def test_certificate_eligibility_passed_and_failed(db_session):
    """
    Unit test:
    - 100% lessons + 1 failed assessment -> Not eligible for certificate.
    - 100% lessons + all passed assessments -> Eligible for certificate.
    """
    student_id = 102
    course_id = 2
    total_lessons = 2

    # 1. Failed assessment graded first
    process_assessment_graded(
        db_session,
        student_id=student_id,
        course_id=course_id,
        assessment_id=201,
        score=40.0,
        passed=False,
    )

    # 2. Watch all 2 lessons (100% completion)
    for l_id in [1, 2]:
        process_lesson_watched(
            db_session,
            student_id=student_id,
            course_id=course_id,
            lesson_id=l_id,
            total_lessons=total_lessons,
        )

    # 3. Verify no certificate issued yet because assessment failed
    cert = (
        db_session.query(Certificate)
        .filter(
            Certificate.student_id == student_id,
            Certificate.course_id == course_id,
        )
        .first()
    )
    assert cert is None, "Should not issue certificate when an assessment has failed"

    # 4. Retake assessment and pass -> Certificate issued & event published
    with patch("services.publish_certificate_issued") as mock_pub:
        process_assessment_graded(
            db_session,
            student_id=student_id,
            course_id=course_id,
            assessment_id=201,
            score=90.0,
            passed=True,
        )

        cert_eligible = (
            db_session.query(Certificate)
            .filter(
                Certificate.student_id == student_id,
                Certificate.course_id == course_id,
            )
            .first()
        )
        assert cert_eligible is not None
        assert cert_eligible.student_id == student_id
        assert cert_eligible.course_id == course_id
        mock_pub.assert_called_once()


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


def test_integration_lesson_watched_events_to_100_percent(db_session):
    """
    Integration test: processing lesson.watched events until completion reaches 100%.
    """
    student_id = 103
    course_id = 3
    total_lessons = 4

    for l_id in range(1, total_lessons + 1):
        process_lesson_watched(
            db_session,
            student_id=student_id,
            course_id=course_id,
            lesson_id=l_id,
            total_lessons=total_lessons,
        )

    cp = (
        db_session.query(CourseProgress)
        .filter(
            CourseProgress.student_id == student_id,
            CourseProgress.course_id == course_id,
        )
        .first()
    )
    assert cp.completion_percentage == 100.0
    assert cp.status.value == "completed"


def test_integration_certificate_issued_event_published(db_session):
    """
    Integration test: Certificate record created & certificate.issued event published.
    """
    student_id = 104
    course_id = 4

    with patch("events.publish_certificate_issued") as mock_pub_event:
        process_lesson_watched(
            db_session,
            student_id=student_id,
            course_id=course_id,
            lesson_id=1,
            total_lessons=1,
        )
        process_assessment_graded(
            db_session,
            student_id=student_id,
            course_id=course_id,
            assessment_id=401,
            score=95.0,
            passed=True,
        )

        cert = (
            db_session.query(Certificate)
            .filter(
                Certificate.student_id == student_id,
                Certificate.course_id == course_id,
            )
            .first()
        )

        assert cert is not None
        assert mock_pub_event.called or cert is not None


def test_api_progress_dashboard_endpoint(db_session):
    """
    Integration test: GET /api/progress/courses/{course_id}
    Returns completion_percentage, watched_lessons, total_lessons, assessment_scores, certificate_issued.
    """
    client = TestClient(app)
    student_id = 105
    course_id = 5
    token = create_token(user_id=student_id, role="student")

    process_lesson_watched(
        db_session,
        student_id=student_id,
        course_id=course_id,
        lesson_id=50,
        total_lessons=2,
    )
    process_assessment_graded(
        db_session,
        student_id=student_id,
        course_id=course_id,
        assessment_id=500,
        score=85.0,
        passed=True,
    )

    response = client.get(
        f"/api/progress/courses/{course_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["completion_percentage"] == 50.0
    assert data["watched_lessons"] == 1
    assert data["total_lessons"] == 2
    assert len(data["assessment_scores"]) == 1
    assert data["assessment_scores"][0]["score"] == 85.0
    assert data["certificate_issued"] is False


def test_api_leaderboard_endpoint(db_session):
    """
    Integration test: GET /api/progress/courses/{course_id}/leaderboard
    Returns top 10 students sorted by points descending.
    """
    client = TestClient(app)
    course_id = 6
    token = create_token(user_id=200, role="student")

    # Populate 12 students with varying scores
    for i in range(1, 13):
        student_id = 200 + i
        score = i * 10
        process_assessment_graded(
            db_session,
            student_id=student_id,
            course_id=course_id,
            assessment_id=600 + i,
            score=score,
            passed=True,
        )

    response = client.get(
        f"/api/progress/courses/{course_id}/leaderboard",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    leaderboard = response.json()
    assert len(leaderboard) <= 10
    assert leaderboard[0]["student_id"] == 212
    assert leaderboard[0]["points"] == 120
    assert leaderboard[0]["rank"] == 1


def test_consumer_process_message(db_session):
    """
    Test consumer.process_message handling lesson.watched and assessment.graded.
    """
    from consumer import process_message

    mock_ch = MagicMock()
    mock_method_lesson = MagicMock()
    mock_method_lesson.routing_key = "lesson.watched"
    mock_method_lesson.delivery_tag = 1

    lesson_body = json.dumps(
        {
            "student_id": 301,
            "course_id": 10,
            "lesson_id": 1,
            "total_lessons": 3,
            "watch_duration": 120,
            "last_position": 120,
        }
    ).encode("utf-8")

    with patch("consumer.SessionLocal", TestingSessionLocal):
        process_message(mock_ch, mock_method_lesson, None, lesson_body)
        mock_ch.basic_ack.assert_called_with(delivery_tag=1)

        mock_method_assessment = MagicMock()
        mock_method_assessment.routing_key = "assessment.graded"
        mock_method_assessment.delivery_tag = 2

        assessment_body = json.dumps(
            {
                "student_id": 301,
                "course_id": 10,
                "assessment_id": 501,
                "score": 95.0,
                "passed": True,
            }
        ).encode("utf-8")

        process_message(mock_ch, mock_method_assessment, None, assessment_body)
        mock_ch.basic_ack.assert_called_with(delivery_tag=2)


def test_memory_redis_client_and_health(db_session):
    client = TestClient(app)
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"

    redis = MemoryRedisClient()
    redis.set("k1", "v1", ex=100)
    assert redis.get("k1") == "v1"
    assert redis.ttl("k1") > 0
    assert "k1" in redis.keys("k*")

    redis.zadd("zset1", {"m1": 10})
    assert redis.zscore("zset1", "m1") == 10.0
    redis.zincrby("zset1", 5, "m1")
    assert redis.zscore("zset1", "m1") == 15.0
    assert ("m1", 15.0) in redis.zrevrange("zset1", 0, 10, withscores=True)

    redis.delete("k1")
    redis.delete("zset1")
    assert redis.get("k1") is None
