"""The local copy of users (kept by the event consumer), the name lookup, and startup."""

import asyncio

import event_consumer
import main
import people
import pytest
from models import ReplicaUser

from shared import events as ev


def names(user_ids):
    return asyncio.run(people.display_names(user_ids))


# ---------------------------------------------------------------------------
# Event consumer
# ---------------------------------------------------------------------------


def test_registration_creates_a_replica_user(session_factory, db):
    event_consumer.handle(
        ev.AUTH_USER_REGISTERED,
        {"user_id": 7, "email": "nimal@example.com", "role": "student"},
    )
    row = db.get(ReplicaUser, 7)
    assert row.email == "nimal@example.com"
    assert row.role == "student"


def test_profile_events_update_and_remove_the_name(session_factory, db):
    event_consumer.handle(
        ev.AUTH_USER_REGISTERED, {"user_id": 7, "email": "nimal@example.com"}
    )
    event_consumer.handle(
        ev.USER_PROFILE_UPSERTED,
        {"user_id": 7, "first_name": "Nimal", "last_name": "Perera"},
    )
    assert db.get(ReplicaUser, 7).full_name == "Nimal Perera"

    event_consumer.handle(ev.USER_PROFILE_DELETED, {"user_id": 7})
    db.expire_all()
    assert db.get(ReplicaUser, 7) is None


def test_account_update_changes_the_email(session_factory, db):
    event_consumer.handle(
        ev.AUTH_USER_REGISTERED, {"user_id": 7, "email": "old@example.com"}
    )
    event_consumer.handle(
        ev.AUTH_ACCOUNT_UPDATED, {"user_id": 7, "email": "new@example.com"}
    )
    assert db.get(ReplicaUser, 7).email == "new@example.com"


def test_events_admin_does_not_keep_are_ignored(session_factory, db):
    event_consumer.handle(ev.COURSE_UPSERTED, {"id": 10, "title": "Maths"})
    assert db.query(ReplicaUser).count() == 0


def test_failed_event_is_rolled_back_and_reraised(monkeypatch):
    calls = []

    class FakeSession:
        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    def boom(*args, **kwargs):
        raise RuntimeError("bad event")

    monkeypatch.setattr(event_consumer, "SessionLocal", FakeSession)
    monkeypatch.setattr(event_consumer.replicas, "apply", boom)

    with pytest.raises(RuntimeError, match="bad event"):
        event_consumer.handle(ev.AUTH_USER_REGISTERED, {"user_id": 7})
    assert calls == ["rollback", "close"]


def test_start_binds_the_admin_queue(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        event_consumer.messaging,
        "start_consumer",
        lambda *args: seen.setdefault("args", args),
    )

    event_consumer.start()

    assert seen["args"] == (
        "admin",
        "admin.queue",
        ev.QUEUES["admin.queue"],
        event_consumer.handle,
    )
    assert ev.AUTH_USER_REGISTERED in ev.QUEUES["admin.queue"]
    assert ev.USER_PROFILE_UPSERTED in ev.QUEUES["admin.queue"]


# ---------------------------------------------------------------------------
# display_names
# ---------------------------------------------------------------------------


def test_no_ids_means_no_database_call(monkeypatch):
    def fail():
        raise AssertionError("database should not be opened")

    monkeypatch.setattr(people, "SessionLocal", fail)
    assert names([]) == {}
    assert names([None, None]) == {}


def test_full_name_wins_over_email(add_user):
    add_user(7, email="nimal@example.com", full_name="Nimal Perera")
    add_user(8, email="kamal@example.com")
    assert names([7, 8]) == {7: "Nimal Perera", 8: "kamal@example.com"}


def test_unknown_and_nameless_users_are_left_out(add_user):
    add_user(9)  # an account with neither a profile name nor an email
    assert names([9, 404]) == {}


def test_duplicate_and_none_ids_are_tolerated(add_user):
    add_user(7, full_name="Nimal Perera")
    assert names([7, None, 7]) == {7: "Nimal Perera"}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def test_startup_creates_tables_and_starts_the_consumer(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main.Base.metadata, "create_all", lambda bind: calls.append("create_all")
    )
    monkeypatch.setattr(main.event_consumer, "start", lambda: calls.append("start"))

    main.on_startup()

    assert calls == ["create_all", "start"]


def test_startup_still_starts_the_consumer_when_the_database_is_down(monkeypatch):
    calls = []

    def down(bind):
        raise ConnectionError("db unreachable")

    monkeypatch.setattr(main.Base.metadata, "create_all", down)
    monkeypatch.setattr(main.event_consumer, "start", lambda: calls.append("start"))

    main.on_startup()

    assert calls == ["start"]
