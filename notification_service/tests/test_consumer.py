import json
from unittest.mock import MagicMock, patch

import pytest
from consumer import process_message
from email_service import EmailDeliveryError
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


def test_given_otp_requested_event_when_consumed_then_otp_email_is_sent():
    # Arrange
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

    # Act and assert
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


def test_given_payment_success_event_when_consumed_then_confirmation_email_is_sent():
    # Arrange
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

    # Act and assert
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


def test_given_certificate_issued_event_when_consumed_then_certificate_email_is_sent():
    # Arrange
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

    # Act and assert
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


def test_given_proctoring_violation_event_when_consumed_then_alert_email_is_sent():
    # Arrange
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

    # Act and assert
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


def test_given_email_sending_fails_when_event_consumed_then_message_is_acknowledged_and_failure_logged():
    # Arrange
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

    # Act and assert
    with patch(
        "consumer.send_email",
        side_effect=EmailDeliveryError("Resend Unreachable"),
    ):
        process_message(mock_ch, mock_method, None, body)

        # The failure is logged and the message acked, not requeued forever.
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=5)
        mock_ch.basic_nack.assert_not_called()

    db = TestingSessionLocal()
    try:
        log = (
            db.query(NotificationLog)
            .filter_by(recipient_email="fail@example.com")
            .first()
        )
        assert log is not None
        assert log.status == NotificationStatus.failed
        assert "Resend Unreachable" in log.error_message
    finally:
        db.close()


def test_given_event_without_email_address_when_consumed_then_message_is_acknowledged_without_processing():
    # Arrange
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 6
    mock_method.routing_key = "payment.success"

    body = json.dumps({"course_title": "No Email Course"})

    # Act and assert
    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        mock_send.assert_not_called()
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=6)


def test_given_unhandled_routing_key_when_consumed_then_message_is_acknowledged():
    # Arrange
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 7
    mock_method.routing_key = "unknown.event"

    body = json.dumps({"email": "test@example.com"})

    # Act and assert
    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        mock_send.assert_not_called()
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=7)


def test_given_consumer_when_started_then_it_binds_the_notification_queue():
    # Act and assert
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
        assert mock_channel.queue_bind.call_count == 9
        mock_channel.start_consuming.assert_called_once()


def test_given_rabbitmq_credentials_in_environment_when_consumer_starts_then_it_connects_with_them(
    monkeypatch,
):
    # Arrange
    monkeypatch.setenv("RABBITMQ_USER", "edura")
    monkeypatch.setenv("RABBITMQ_PASSWORD", "s3cret")

    # Act
    with patch("pika.BlockingConnection") as mock_pika_conn:
        from consumer import start_consumer

        start_consumer()

    # Assert
    credentials = mock_pika_conn.call_args.args[0].credentials
    assert (credentials.username, credentials.password) == ("edura", "s3cret")


def test_given_progress_report_event_when_consumed_then_email_has_a_pdf_attached():
    # Arrange
    import base64

    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 9
    mock_method.routing_key = "report.progress"

    body = json.dumps(
        {
            "user_id": 7,
            "email": "nimal@example.com",
            "student_name": "Nimal",
            "month": "October 2026",
            "filename": "Progress_Report_Nimal_20261010.pdf",
            "pdf_base64": base64.b64encode(b"%PDF-1.4 report").decode(),
        }
    )

    # Act and assert
    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        kwargs = mock_send.call_args.kwargs
        assert kwargs["to_email"] == "nimal@example.com"
        assert kwargs["subject"] == "Your Edura Progress Report - October 2026"
        assert "Nimal" in kwargs["html_body"]
        (attachment,) = kwargs["attachments"]
        assert attachment["filename"] == "Progress_Report_Nimal_20261010.pdf"
        assert attachment["data"] == b"%PDF-1.4 report"
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=9)

    db = TestingSessionLocal()
    try:
        log = (
            db.query(NotificationLog)
            .filter_by(recipient_email="nimal@example.com")
            .first()
        )
        assert log.event_type == NotificationEvent.progress_report
        assert log.status == NotificationStatus.sent
        assert (
            "%PDF" not in log.body
        )  # the log keeps the email body, not the attachment
    finally:
        db.close()


def test_given_email_verification_event_when_consumed_then_verification_link_is_sent():
    # Arrange
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 11
    mock_method.routing_key = "auth.email-verification-requested"

    body = json.dumps(
        {
            "user_id": 61,
            "email": "newbie@example.com",
            "verify_url": "http://localhost:3000/verify-email?token=abc.def.ghi",
            "expires_in_minutes": 1440,
        }
    )

    # Act and assert
    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        kwargs = mock_send.call_args.kwargs
        assert kwargs["to_email"] == "newbie@example.com"
        assert kwargs["subject"] == "Verify your Edura email address"
        assert "verify-email?token=abc.def.ghi" in kwargs["html_body"]
        assert "24 hours" in kwargs["html_body"]
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=11)

    db = TestingSessionLocal()
    try:
        log = (
            db.query(NotificationLog)
            .filter_by(recipient_email="newbie@example.com")
            .first()
        )
        assert log.event_type == NotificationEvent.email_verification_requested
        assert log.status == NotificationStatus.sent
    finally:
        db.close()


def test_given_password_reset_event_when_consumed_then_email_includes_the_reset_link():
    # Arrange
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 12
    mock_method.routing_key = "auth.password-reset-requested"

    body = json.dumps(
        {
            "user_id": 77,
            "email": "forgetful@example.com",
            "reset_url": "http://localhost:3000/reset-password?token=xyz.123",
            "expires_in_minutes": 60,
        }
    )

    # Act and assert
    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        kwargs = mock_send.call_args.kwargs
        assert kwargs["to_email"] == "forgetful@example.com"
        assert kwargs["subject"] == "Reset your Edura password"
        assert "reset-password?token=xyz.123" in kwargs["html_body"]
        assert "copy this link into your browser" in kwargs["html_body"]
        assert "60 minutes" in kwargs["html_body"]
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=12)


def test_given_password_reset_event_without_optional_fields_when_consumed_then_defaults_are_used():
    # Arrange
    mock_ch = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 13
    mock_method.routing_key = "auth.password-reset-requested"

    body = json.dumps({"user_id": 78, "email": "defaults@example.com"})

    # Act and assert
    with patch("consumer.send_email") as mock_send:
        process_message(mock_ch, mock_method, None, body)

        kwargs = mock_send.call_args.kwargs
        assert kwargs["to_email"] == "defaults@example.com"
        assert 'href="#"' in kwargs["html_body"]
        assert "60 minutes" in kwargs["html_body"]
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=13)

    db = TestingSessionLocal()
    try:
        log = (
            db.query(NotificationLog)
            .filter_by(recipient_email="defaults@example.com")
            .first()
        )
        assert log is not None
        assert log.event_type == NotificationEvent.password_reset_requested
        assert log.status == NotificationStatus.sent
        assert log.recipient_id == 78
    finally:
        db.close()
