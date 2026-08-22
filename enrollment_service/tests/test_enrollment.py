import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Setup test DB (SQLite in-memory)
TEST_DATABASE_URL = "sqlite:///./test_enrollment_db.db"

os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import database
from database import Base, get_db
import models
from models import Enrollment, EnrollmentStatus
import services

# Create SQLite engine
engine = create_engine(
    TEST_DATABASE_URL, connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=engine
)

# Patch SessionLocal in database module and consumer
database.engine = engine
database.SessionLocal = TestingSessionLocal


Base.metadata.create_all(bind=engine)

from main import app
import consumer

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    if os.path.exists("./test_enrollment_db.db"):
        try:
            os.remove("./test_enrollment_db.db")
        except Exception:
            pass



# ---------------------------------------------------------------------------
# Test AC2: GET /api/enrollments Query Endpoint
# ---------------------------------------------------------------------------
def test_get_enrollment_success():
    db = TestingSessionLocal()
    enrollment = services.activate_or_extend_enrollment(
        db, student_id=10, course_id=20
    )

    r = client.get("/api/enrollments?student_id=10&course_id=20")

    assert r.status_code == 200
    data = r.json()
    assert data["enrollment_id"] == enrollment.id
    assert data["student_id"] == 10
    assert data["course_id"] == 20
    assert data["status"] == "ACTIVE"
    assert data["expires_at"] is not None


def test_get_enrollment_not_found_returns_404():
    r = client.get("/api/enrollments?student_id=99&course_id=99")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test AC1 & AC4: RabbitMQ payment.success consumer and duplication extension
# ---------------------------------------------------------------------------
@patch("consumer.publish_enrollment_activated")
def test_consumer_payment_success_activates_enrollment(mock_publish):
    db_session = TestingSessionLocal()
    with patch("consumer.SessionLocal", return_value=db_session):
        ch_mock = MagicMock()
        method_mock = MagicMock()
        method_mock.routing_key = "payment.success"
        method_mock.delivery_tag = 1

        body = '{"student_id": 1, "course_id": 1, "order_id": "ORD-123"}'

        consumer.process_message(ch_mock, method_mock, None, body)

        # Assert DB record created
        enrollment = (
            db_session.query(Enrollment)
            .filter(Enrollment.student_id == 1, Enrollment.course_id == 1)
            .first()
        )
        assert enrollment is not None
        assert enrollment.status == EnrollmentStatus.ACTIVE
        assert enrollment.expires_at is not None

        # Assert event published and message acknowledged
        mock_publish.assert_called_once()
        ch_mock.basic_ack.assert_called_once_with(delivery_tag=1)


@patch("consumer.publish_enrollment_activated")
def test_consumer_duplicate_payment_success_extends_expiry(mock_publish):
    db_session = TestingSessionLocal()
    with patch("consumer.SessionLocal", return_value=db_session):
        ch_mock = MagicMock()
        method_mock = MagicMock()
        method_mock.routing_key = "payment.success"
        method_mock.delivery_tag = 1

        body = '{"student_id": 5, "course_id": 5, "order_id": "ORD-001"}'

        # First payment event
        consumer.process_message(ch_mock, method_mock, None, body)
        enrollment1 = (
            db_session.query(Enrollment)
            .filter(Enrollment.student_id == 5, Enrollment.course_id == 5)
            .first()
        )
        first_exp = enrollment1.expires_at

        # Second payment event for same student + course
        body2 = '{"student_id": 5, "course_id": 5, "order_id": "ORD-002"}'
        consumer.process_message(ch_mock, method_mock, None, body2)

        # Check that ONLY ONE record exists
        count = (
            db_session.query(Enrollment)
            .filter(Enrollment.student_id == 5, Enrollment.course_id == 5)
            .count()
        )
        assert count == 1

        enrollment2 = (
            db_session.query(Enrollment)
            .filter(Enrollment.student_id == 5, Enrollment.course_id == 5)
            .first()
        )
        # Expiration date extended
        assert enrollment2.expires_at > first_exp


# ---------------------------------------------------------------------------
# Test AC3: subscription.expired event suspends access
# ---------------------------------------------------------------------------
def test_consumer_subscription_expired_suspends_access():
    db_session = TestingSessionLocal()
    # First create an active enrollment
    services.activate_or_extend_enrollment(db_session, student_id=7, course_id=7)

    with patch("consumer.SessionLocal", return_value=db_session):
        ch_mock = MagicMock()
        method_mock = MagicMock()
        method_mock.routing_key = "subscription.expired"
        method_mock.delivery_tag = 2

        body = '{"student_id": 7, "course_id": 7}'

        consumer.process_message(ch_mock, method_mock, None, body)

        enrollment = (
            db_session.query(Enrollment)
            .filter(Enrollment.student_id == 7, Enrollment.course_id == 7)
            .first()
        )
        assert enrollment.status == EnrollmentStatus.SUSPENDED
        ch_mock.basic_ack.assert_called_once_with(delivery_tag=2)
