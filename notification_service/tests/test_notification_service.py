"""Notification service gaps: the password-changed email, poison messages, the
consumer's reconnect loop, database preparation, Resend's non-JSON errors and
the small HTTP app (health, startup, role-gated route)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import consumer
import email_service
import jwt
import main
import pytest
from fastapi.testclient import TestClient
from models import Base, NotificationEvent, NotificationLog, NotificationStatus
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import auth

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(consumer, "SessionLocal", TestingSessionLocal)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def mailed(monkeypatch):
    sent = []
    monkeypatch.setattr(consumer, "send_email", lambda **kw: sent.append(kw))
    return sent


def _deliver(routing_key: str, body):
    ch = MagicMock()
    method = SimpleNamespace(routing_key=routing_key, delivery_tag=11)
    raw = body if isinstance(body, (str, bytes)) else json.dumps(body)
    consumer.process_message(ch, method, None, raw)
    return ch


def _logs():
    db = TestingSessionLocal()
    try:
        return db.query(NotificationLog).all()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------


def test_given_password_changed_event_when_delivered_then_message_is_acknowledged_and_email_sent(
    mailed,
):
    # Act
    ch = _deliver("auth.password-changed", {"user_id": 7, "email": "ama@example.com"})

    # Assert
    ch.basic_ack.assert_called_once_with(delivery_tag=11)
    (mail,) = mailed

    # Act and assert
    assert mail["subject"] == "Your Edura password was changed"
    assert mail["attachments"] == []

    # Act
    (log,) = _logs()

    # Assert
    assert (log.event_type, log.status, log.recipient_id) == (
        NotificationEvent.password_changed,
        NotificationStatus.sent,
        7,
    )


def test_given_event_with_student_email_and_id_fields_when_consumed_then_they_are_accepted(
    mailed,
):
    # Arrange
    _deliver(
        "payment.success",
        {"student_email": "kid@example.com", "student_id": 9, "order_id": "ORD-1"},
    )

    # Act and assert
    assert mailed[0]["to_email"] == "kid@example.com"
    assert _logs()[0].recipient_id == 9


def test_given_link_valid_for_twenty_minutes_when_email_rendered_then_lifetime_reads_as_one_hour(
    mailed,
):
    # Act
    _deliver(
        "auth.email-verification-requested",
        {
            "email": "a@example.com",
            "verify_url": "https://x/v",
            "expires_in_minutes": 20,
        },
    )
    # Under an hour still reads as "1 hour", never "0 hours".
    # Assert
    assert "1" in mailed[0]["html_body"]


def test_given_progress_report_without_month_when_subject_built_then_subject_has_no_month(
    mailed,
):
    # Act
    _deliver("report.progress", {"email": "a@example.com", "pdf_base64": ""})

    # Assert
    assert mailed[0]["subject"] == "Your Edura Progress Report"
    assert mailed[0]["attachments"][0]["filename"] == "Progress_Report.pdf"


@pytest.mark.parametrize(
    "routing_key, body",
    [
        ("auth.password-changed", "this is not json"),
        ("report.progress", {"email": "a@example.com"}),  # pdf_base64 missing
    ],
)
def test_given_broken_message_when_consumed_then_it_is_dropped_and_not_requeued(
    mailed, routing_key, body
):
    # Arrange
    ch = _deliver(routing_key, body)

    # Act and assert
    ch.basic_nack.assert_called_once_with(delivery_tag=11, requeue=False)
    ch.basic_ack.assert_not_called()
    assert mailed == [] and _logs() == []


def test_given_unreachable_broker_when_consumer_runs_then_it_retries_until_broker_is_reachable(
    monkeypatch,
):
    # Arrange
    attempts = []

    # Act
    channel = MagicMock()

    def connect(params):
        attempts.append(params)
        if len(attempts) < 3:
            raise ConnectionError("broker starting")
        conn = MagicMock()
        conn.channel.return_value = channel
        return conn

    sleeps = []
    monkeypatch.setattr(consumer.pika, "BlockingConnection", connect)
    monkeypatch.setattr(consumer.time, "sleep", lambda s: sleeps.append(s))

    consumer.start_consumer()

    # Assert
    assert len(attempts) == 3 and sleeps == [5, 5]
    channel.basic_consume.assert_called_once_with(
        queue="notification.queue", on_message_callback=consumer.process_message
    )
    bound = {c.kwargs["routing_key"] for c in channel.queue_bind.call_args_list}

    # Act and assert
    assert {"auth.password-changed", "report.progress", "violation.alert"} <= bound


def test_given_empty_database_when_prepared_then_tables_are_created_and_sqlite_is_tolerated(
    monkeypatch,
):
    # Arrange
    fresh = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(consumer, "engine", fresh)
    consumer.prepare_database()  # ALTER TYPE is Postgres-only; logged, not fatal

    # Act and assert
    with fresh.connect() as conn:
        assert "notification_logs" in fresh.dialect.get_table_names(conn)
    fresh.dispose()


def test_given_table_creation_fails_when_database_prepared_then_service_still_starts(
    monkeypatch,
):
    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(consumer.Base.metadata, "create_all", broken)
    monkeypatch.setattr(consumer, "engine", engine)
    consumer.prepare_database()


def test_given_postgres_when_database_prepared_then_enum_migrations_run_outside_a_transaction(
    monkeypatch,
):
    # Arrange
    executed = []

    class Conn:
        def execution_options(self, **opts):
            executed.append(("options", opts))
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, stmt):
            executed.append(str(stmt))

    monkeypatch.setattr(consumer.Base.metadata, "create_all", lambda **kw: None)
    monkeypatch.setattr(consumer, "engine", SimpleNamespace(connect=Conn))

    # Act
    consumer.prepare_database()

    # Assert
    assert executed[0] == ("options", {"isolation_level": "AUTOCOMMIT"})
    assert all("ADD VALUE IF NOT EXISTS" in s for s in executed[1:])
    assert len(executed) == 5


# ---------------------------------------------------------------------------
# Resend
# ---------------------------------------------------------------------------


def test_given_resend_error_that_is_not_json_when_email_sent_then_delivery_error_is_raised(
    monkeypatch,
):
    # Arrange
    class Response:
        status_code = 502
        text = "Bad Gateway"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(email_service, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(email_service.httpx, "post", lambda *a, **k: Response())

    # Act and assert
    with pytest.raises(email_service.EmailDeliveryError, match="502.*Bad Gateway"):
        email_service.send_email("a@example.com", "Hi", "<p>Hi</p>")


def test_given_resend_error_without_message_when_email_sent_then_response_body_is_used(
    monkeypatch,
):
    # Arrange
    class Response:
        status_code = 422
        text = "invalid from"

        def json(self):
            return {}

    monkeypatch.setattr(email_service, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(email_service.httpx, "post", lambda *a, **k: Response())

    # Act and assert
    with pytest.raises(email_service.EmailDeliveryError, match="invalid from"):
        email_service.send_email("a@example.com", "Hi", "<p>Hi</p>")


# ---------------------------------------------------------------------------
# HTTP app
# ---------------------------------------------------------------------------

client = TestClient(main.app)


def _auth(role: str) -> dict:
    token = jwt.encode(
        {"sub": "1", "role": role}, auth.SECRET_KEY, algorithm=auth.ALGORITHM
    )
    return {"Authorization": f"Bearer {token}"}


def test_given_running_service_when_health_requested_then_reports_ok():
    # Act and assert
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "notification_service",
    }


def test_given_non_manager_or_no_token_when_notification_route_called_then_403_or_401():
    # Act and assert
    assert client.get(
        "/notification", headers=_auth("notification_manager")
    ).json() == {"message": "Welcome Notification Manager"}
    assert client.get("/notification", headers=_auth("admin")).status_code == 403
    assert client.get("/notification").status_code == 401


def test_given_service_starting_when_startup_runs_then_tables_are_created(monkeypatch):
    # Arrange
    fresh = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(main, "engine", fresh)
    main.on_startup()

    # Act and assert
    with fresh.connect() as conn:
        assert "notification_logs" in fresh.dialect.get_table_names(conn)
    fresh.dispose()


def test_given_table_creation_fails_when_startup_runs_then_service_still_starts(
    monkeypatch,
):
    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    main.on_startup()
