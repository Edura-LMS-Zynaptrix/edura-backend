import json
import smtplib
from unittest.mock import MagicMock, patch

import pytest
from consumer import process_message
from models import Base, NotificationEvent, NotificationLog, NotificationStatus
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

test_engine = create_engine("sqlite:///:memory:")
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    Base.metadata.create_all(bind=test_engine)
    monkeypatch.setattr("consumer.SessionLocal", TestingSessionLocal)
    yield
    Base.metadata.drop_all(bind=test_engine)


def test_process_otp_requested_success():
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 1
    mock_method.routing_key = "auth.otp-requested"

    body = json.dumps(
        {
            "email": "student@example.com",
            "otp_code": "654321",
            "expires_in_minutes": 5,
            "user_id": 42,
        }
    )

    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert kwargs["to_email"] == "student@example.com"
        assert "654321" in kwargs["html_body"]
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=1)

    db = TestingSessionLocal()
    try:
        log = (
            db.query(NotificationLog)
            .filter_by(recipient_email="student@example.com")
            .first()
        )
        assert log is not None
        assert log.status == NotificationStatus.sent
        assert log.event_type == NotificationEvent.otp_requested
        assert log.recipient_id == 42
    finally:
        db.close()


def test_process_payment_success():
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 2
    mock_method.routing_key = "payment.success"

    body = json.dumps(
        {
            "student_email": "payee@example.com",
            "course_title": "Python Microservices",
            "amount": 99.99,
            "order_id": "ORD-12345",
            "student_id": 101,
        }
    )

    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        mock_send.assert_called_once()
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=2)

    db = TestingSessionLocal()
    try:
        log = (
            db.query(NotificationLog)
            .filter_by(recipient_email="payee@example.com")
            .first()
        )
        assert log is not None
        assert log.status == NotificationStatus.sent
        assert log.event_type == NotificationEvent.payment_received
        assert log.recipient_id == 101
        assert "ORD-12345" in log.body
    finally:
        db.close()


def test_process_certificate_issued():
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 3
    mock_method.routing_key = "certificate.issued"

    body = json.dumps(
        {
            "student_email": "grad@example.com",
            "student_name": "Alice Smith",
            "course_title": "Data Science Masterclass",
            "certificate_id": 888,
            "student_id": 202,
        }
    )

    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        mock_send.assert_called_once()
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=3)

    db = TestingSessionLocal()
    try:
        log = (
            db.query(NotificationLog)
            .filter_by(recipient_email="grad@example.com")
            .first()
        )
        assert log is not None
        assert log.status == NotificationStatus.sent
        assert log.event_type == NotificationEvent.certificate_issued
        assert log.reference_id == 888
    finally:
        db.close()


def test_process_proctoring_violation():
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 4
    mock_method.routing_key = "proctoring.violation"

    body = json.dumps(
        {
            "student_email": "alert@example.com",
            "violation_details": "Tab switching detected 3 times.",
            "student_id": 303,
        }
    )

    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        mock_send.assert_called_once()
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=4)

    db = TestingSessionLocal()
    try:
        log = (
            db.query(NotificationLog)
            .filter_by(recipient_email="alert@example.com")
            .first()
        )
        assert log is not None
        assert log.status == NotificationStatus.sent
        assert log.event_type == NotificationEvent.proctoring_violation
    finally:
        db.close()


def test_smtp_failure_nacks_and_logs_failed():
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 5
    mock_method.routing_key = "payment.success"

    body = json.dumps(
        {
            "student_email": "fail@example.com",
            "course_title": "Docker Essentials",
            "amount": 49.99,
            "order_id": "ORD-FAIL-1",
            "student_id": 404,
        }
    )

    with patch(
        "consumer.send_email",
        side_effect=smtplib.SMTPException("SMTP Server Unreachable"),
    ):
        process_message(mock_ch, mock_method, None, body)

        mock_ch.basic_nack.assert_called_once_with(delivery_tag=5, requeue=True)
        mock_ch.basic_ack.assert_not_called()

    db = TestingSessionLocal()
    try:
        log = (
            db.query(NotificationLog)
            .filter_by(recipient_email="fail@example.com")
            .first()
        )
        assert log is not None
        assert log.status == NotificationStatus.failed
        assert "SMTP Server Unreachable" in log.error_message
    finally:
        db.close()


def test_missing_email_acks_without_processing():
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 6
    mock_method.routing_key = "payment.success"

    body = json.dumps({"course_title": "No Email Course"})

    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        mock_send.assert_not_called()
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=6)


def test_unhandled_routing_key_acks():
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 7
    mock_method.routing_key = "unknown.event"

    body = json.dumps({"email": "test@example.com"})

    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        mock_send.assert_not_called()
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=7)


def test_start_consumer():
    with patch("pika.BlockingConnection") as mock_pika_conn:
        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_pika_conn.return_value = mock_conn
        mock_conn.channel.return_value = mock_channel

        from consumer import start_consumer

        start_consumer()

        mock_pika_conn.assert_called_once()
        mock_channel.exchange_declare.assert_called_once()
        mock_channel.queue_declare.assert_called_once()
        assert mock_channel.queue_bind.call_count == 5
        mock_channel.start_consuming.assert_called_once()
