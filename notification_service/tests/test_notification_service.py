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


def test_password_changed_email(mailed):
    ch = _deliver("auth.password-changed", {"user_id": 7, "email": "ama@example.com"})
    ch.basic_ack.assert_called_once_with(delivery_tag=11)
    (mail,) = mailed
    assert mail["subject"] == "Your Edura password was changed"
    assert mail["attachments"] == []
    (log,) = _logs()
    assert (log.event_type, log.status, log.recipient_id) == (
        NotificationEvent.password_changed,
        NotificationStatus.sent,
        7,
    )


def test_student_email_and_id_fields_are_accepted(mailed):
    _deliver(
        "payment.success",
        {"student_email": "kid@example.com", "student_id": 9, "order_id": "ORD-1"},
    )
    assert mailed[0]["to_email"] == "kid@example.com"
    assert _logs()[0].recipient_id == 9


def test_verification_link_lifetime_is_shown_in_hours(mailed):
    _deliver(
        "auth.email-verification-requested",
        {
            "email": "a@example.com",
            "verify_url": "https://x/v",
            "expires_in_minutes": 20,
        },
    )
    # Under an hour still reads as "1 hour", never "0 hours".
    assert "1" in mailed[0]["html_body"]


def test_progress_report_subject_without_a_month(mailed):
    _deliver("report.progress", {"email": "a@example.com", "pdf_base64": ""})
    assert mailed[0]["subject"] == "Your Edura Progress Report"
    assert mailed[0]["attachments"][0]["filename"] == "Progress_Report.pdf"


@pytest.mark.parametrize(
    "routing_key, body",
    [
        ("auth.password-changed", "this is not json"),
        ("report.progress", {"email": "a@example.com"}),  # pdf_base64 missing
    ],
)
def test_broken_messages_are_dropped_not_requeued(mailed, routing_key, body):
    ch = _deliver(routing_key, body)
    ch.basic_nack.assert_called_once_with(delivery_tag=11, requeue=False)
    ch.basic_ack.assert_not_called()
    assert mailed == [] and _logs() == []


def test_consumer_retries_until_the_broker_is_reachable(monkeypatch):
    attempts = []
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

    assert len(attempts) == 3 and sleeps == [5, 5]
    channel.basic_consume.assert_called_once_with(
        queue="notification.queue", on_message_callback=consumer.process_message
    )
    bound = {c.kwargs["routing_key"] for c in channel.queue_bind.call_args_list}
    assert {"auth.password-changed", "report.progress", "violation.alert"} <= bound


def test_prepare_database_creates_tables_and_tolerates_sqlite(monkeypatch):
    fresh = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(consumer, "engine", fresh)
    consumer.prepare_database()  # ALTER TYPE is Postgres-only; logged, not fatal
    with fresh.connect() as conn:
        assert "notification_logs" in fresh.dialect.get_table_names(conn)
    fresh.dispose()


def test_prepare_database_survives_create_all_failure(monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(consumer.Base.metadata, "create_all", broken)
    monkeypatch.setattr(consumer, "engine", engine)
    consumer.prepare_database()


def test_enum_migrations_run_on_postgres(monkeypatch):
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
    consumer.prepare_database()
    assert executed[0] == ("options", {"isolation_level": "AUTOCOMMIT"})
    assert all("ADD VALUE IF NOT EXISTS" in s for s in executed[1:])
    assert len(executed) == 5


# ---------------------------------------------------------------------------
# Resend
# ---------------------------------------------------------------------------


def test_resend_error_that_is_not_json(monkeypatch):
    class Response:
        status_code = 502
        text = "Bad Gateway"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(email_service, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(email_service.httpx, "post", lambda *a, **k: Response())
    with pytest.raises(email_service.EmailDeliveryError, match="502.*Bad Gateway"):
        email_service.send_email("a@example.com", "Hi", "<p>Hi</p>")


def test_resend_error_without_a_message_uses_the_body(monkeypatch):
    class Response:
        status_code = 422
        text = "invalid from"

        def json(self):
            return {}

    monkeypatch.setattr(email_service, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(email_service.httpx, "post", lambda *a, **k: Response())
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


def test_health():
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "notification_service",
    }


def test_notification_route_is_for_notification_managers():
    assert client.get(
        "/notification", headers=_auth("notification_manager")
    ).json() == {"message": "Welcome Notification Manager"}
    assert client.get("/notification", headers=_auth("admin")).status_code == 403
    assert client.get("/notification").status_code == 401


def test_startup_creates_tables(monkeypatch):
    fresh = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(main, "engine", fresh)
    main.on_startup()
    with fresh.connect() as conn:
        assert "notification_logs" in fresh.dialect.get_table_names(conn)
    fresh.dispose()


def test_startup_survives_create_all_failure(monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    main.on_startup()
