import asyncio
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# SQLite in-memory with StaticPool so every session sees the same database.
# DATABASE_URL is left alone on purpose: test_migrations reads it at import.
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

import database

# people.py and event_consumer.py bind SessionLocal at import, so patch first.
database.engine = engine
database.SessionLocal = TestingSessionLocal

import event_consumer
import main
import people
from database import Base, get_db
from main import app
from models import ChatConversation, ChatMessage, Feedback, ReplicaUser

from shared import events as ev
from shared.auth import ALGORITHM, SECRET_KEY


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)

GUEST_SESSION = "guest-session-0001"


def _auth(user_id: int, role: str = "student", **claims) -> dict:
    token = jwt.encode(
        {"sub": str(user_id), "role": role, **claims}, SECRET_KEY, algorithm=ALGORITHM
    )
    return {"Authorization": f"Bearer {token}"}


ADMIN = _auth(1, "admin")
STUDENT = _auth(10)
OTHER_STUDENT = _auth(11)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _at(minutes: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def _add_conversation(db, **fields) -> ChatConversation:
    conv = ChatConversation(**fields)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _add_message(db, conversation_id: int, message: str, **fields) -> ChatMessage:
    msg = ChatMessage(conversation_id=conversation_id, message=message, **fields)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _add_replica_user(db, user_id: int, **fields) -> None:
    db.add(ReplicaUser(user_id=user_id, **fields))
    db.commit()


def _feedback_body(**overrides) -> dict:
    body = {
        "name": "Nimal Perera",
        "email": "nimal@example.com",
        "category": "suggestion",
        "subject": "Dark mode",
        "message": "Please add a dark theme.",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "admin_service"}


def test_startup_creates_tables_and_starts_consumer(monkeypatch):
    calls = []
    Base.metadata.drop_all(bind=engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("start"))

    main.on_startup()

    assert calls == ["start"]
    assert TestingSessionLocal().query(Feedback).count() == 0


def test_startup_still_starts_consumer_when_db_unavailable(monkeypatch):
    calls = []

    def broken_create_all(bind):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken_create_all)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("start"))

    main.on_startup()

    assert calls == ["start"]


# ---------------------------------------------------------------------------
# /admin and role checks
# ---------------------------------------------------------------------------


def test_admin_endpoint_welcomes_admin():
    resp = client.get("/admin", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json() == {"message": "Welcome Admin"}


def test_admin_role_is_case_insensitive():
    resp = client.get("/admin", headers=_auth(1, "ADMIN"))
    assert resp.status_code == 200


@pytest.mark.parametrize("role", ["student", "teacher"])
def test_admin_endpoint_forbids_other_roles(role):
    resp = client.get("/admin", headers=_auth(5, role))
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


def test_admin_endpoint_requires_token():
    resp = client.get("/admin")
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "MISSING_TOKEN"


def test_admin_endpoint_rejects_expired_token():
    expired = _auth(1, "admin", exp=datetime.now(timezone.utc) - timedelta(minutes=1))
    resp = client.get("/admin", headers=expired)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "TOKEN_EXPIRED"


def test_admin_endpoint_rejects_bad_signature():
    token = jwt.encode({"sub": "1", "role": "admin"}, "wrong-key", algorithm=ALGORITHM)
    resp = client.get("/admin", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "INVALID_TOKEN"


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


def test_guest_submits_feedback():
    resp = client.post("/feedback/submit", json=_feedback_body())
    assert resp.status_code == 201
    data = resp.json()
    assert data["user_id"] is None
    assert data["name"] == "Nimal Perera"
    assert data["category"] == "suggestion"
    assert data["is_read"] is False
    assert data["is_resolved"] is False


def test_signed_in_feedback_is_linked_to_user():
    resp = client.post("/feedback/submit", json=_feedback_body(), headers=STUDENT)
    assert resp.status_code == 201
    assert resp.json()["user_id"] == 10


def test_feedback_with_invalid_token_is_saved_as_guest():
    resp = client.post(
        "/feedback/submit",
        json=_feedback_body(),
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert resp.status_code == 201
    assert resp.json()["user_id"] is None


def test_feedback_text_is_trimmed_and_blank_subject_dropped():
    resp = client.post(
        "/feedback/submit",
        json=_feedback_body(name="  Kamal  ", subject="   ", message="  Hi  "),
    )
    data = resp.json()
    assert data["name"] == "Kamal"
    assert data["subject"] is None
    assert data["message"] == "Hi"


def test_feedback_subject_is_optional():
    body = _feedback_body()
    del body["subject"]
    resp = client.post("/feedback/submit", json=body)
    assert resp.status_code == 201
    assert resp.json()["subject"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "not-an-email"},
        {"category": "complaint"},
        {"name": ""},
        {"name": "x" * 121},
        {"message": ""},
        {"message": "x" * 5001},
        {"subject": "x" * 201},
    ],
)
def test_feedback_validation(overrides):
    resp = client.post("/feedback/submit", json=_feedback_body(**overrides))
    assert resp.status_code == 422


def test_list_feedback_newest_first(db):
    db.add_all(
        [
            Feedback(
                name="Old",
                email="a@example.com",
                category="bug",
                message="m",
                created_at=_at(0),
            ),
            Feedback(
                name="New",
                email="b@example.com",
                category="other",
                message="m",
                created_at=_at(5),
            ),
        ]
    )
    db.commit()

    resp = client.get("/feedback/admin/all", headers=ADMIN)
    assert resp.status_code == 200
    assert [f["name"] for f in resp.json()] == ["New", "Old"]


def test_list_feedback_paginates():
    for i in range(3):
        client.post("/feedback/submit", json=_feedback_body(name=f"User {i}"))

    resp = client.get("/feedback/admin/all?skip=1&limit=1", headers=ADMIN)
    assert resp.status_code == 200
    # Same created_at second in SQLite, so the id tiebreak decides: 3, 2, 1.
    assert [f["name"] for f in resp.json()] == ["User 1"]


@pytest.mark.parametrize("query", ["skip=-1", "limit=0", "limit=501"])
def test_list_feedback_rejects_bad_paging(query):
    resp = client.get(f"/feedback/admin/all?{query}", headers=ADMIN)
    assert resp.status_code == 422


def test_list_feedback_requires_admin():
    assert client.get("/feedback/admin/all").status_code == 401
    assert client.get("/feedback/admin/all", headers=STUDENT).status_code == 403


def test_mark_feedback_read():
    fid = client.post("/feedback/submit", json=_feedback_body()).json()["id"]

    resp = client.patch(f"/feedback/admin/{fid}/read", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["is_read"] is True


def test_mark_feedback_read_not_found():
    resp = client.patch("/feedback/admin/999/read", headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "FEEDBACK_NOT_FOUND"


def test_mark_feedback_read_requires_admin():
    fid = client.post("/feedback/submit", json=_feedback_body()).json()["id"]
    resp = client.patch(f"/feedback/admin/{fid}/read", headers=STUDENT)
    assert resp.status_code == 403


def test_delete_feedback(db):
    fid = client.post("/feedback/submit", json=_feedback_body()).json()["id"]

    resp = client.delete(f"/feedback/admin/{fid}", headers=ADMIN)
    assert resp.status_code == 204
    assert db.get(Feedback, fid) is None


def test_delete_feedback_not_found():
    resp = client.delete("/feedback/admin/999", headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "FEEDBACK_NOT_FOUND"


def test_delete_feedback_requires_admin(db):
    fid = client.post("/feedback/submit", json=_feedback_body()).json()["id"]
    resp = client.delete(f"/feedback/admin/{fid}", headers=STUDENT)
    assert resp.status_code == 403
    assert db.get(Feedback, fid) is not None


# ---------------------------------------------------------------------------
# Chat — student / guest side
# ---------------------------------------------------------------------------


def test_send_requires_identity():
    resp = client.post("/chat/send", json={"message": "hello"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "IDENTITY_REQUIRED"


def test_guest_send_starts_and_reuses_thread(db):
    first = client.post(
        "/chat/send", json={"message": "  hello  ", "session_id": GUEST_SESSION}
    )
    assert first.status_code == 200
    data = first.json()
    assert data["message"] == "hello"
    assert data["sender_id"] is None
    assert data["is_from_admin"] is False

    second = client.post(
        "/chat/send", json={"message": "again", "session_id": GUEST_SESSION}
    )
    assert second.json()["conversation_id"] == data["conversation_id"]

    conv = db.get(ChatConversation, data["conversation_id"])
    assert conv.session_id == GUEST_SESSION
    assert conv.user_id is None


def test_signed_in_send_starts_user_thread(db):
    resp = client.post(
        "/chat/send",
        json={"message": "hi", "session_id": GUEST_SESSION},
        headers=STUDENT,
    )
    assert resp.status_code == 200
    assert resp.json()["sender_id"] == 10

    conv = db.get(ChatConversation, resp.json()["conversation_id"])
    assert conv.user_id == 10
    # A signed-in thread is keyed by the user, not the browser session.
    assert conv.session_id is None


def test_signed_in_send_reuses_own_thread():
    first = client.post("/chat/send", json={"message": "one"}, headers=STUDENT)
    second = client.post("/chat/send", json={"message": "two"}, headers=STUDENT)
    assert first.json()["conversation_id"] == second.json()["conversation_id"]


def test_signed_in_user_continues_unclaimed_guest_thread(db):
    guest = _add_conversation(db, session_id=GUEST_SESSION)

    resp = client.post(
        "/chat/send",
        json={"message": "signed in now", "session_id": GUEST_SESSION},
        headers=STUDENT,
    )
    assert resp.json()["conversation_id"] == guest.id


def test_signed_in_user_cannot_use_another_users_thread(db):
    claimed = _add_conversation(db, user_id=10, session_id=GUEST_SESSION)

    resp = client.post(
        "/chat/send",
        json={"message": "hijack", "session_id": GUEST_SESSION},
        headers=OTHER_STUDENT,
    )
    assert resp.status_code == 200
    assert resp.json()["conversation_id"] != claimed.id


def test_send_updates_last_message_at(db):
    conv = _add_conversation(db, session_id=GUEST_SESSION, last_message_at=_at(0))

    client.post("/chat/send", json={"message": "hi", "session_id": GUEST_SESSION})

    db.refresh(conv)
    assert conv.last_message_at.replace(tzinfo=timezone.utc) > _at(0)


@pytest.mark.parametrize(
    "body",
    [
        {"message": "", "session_id": GUEST_SESSION},
        {"message": "x" * 4001, "session_id": GUEST_SESSION},
        {"message": "hi", "session_id": "short"},
        {"message": "hi", "session_id": "bad session id!"},
        {"message": "hi", "session_id": "x" * 65},
    ],
)
def test_send_validation(body):
    assert client.post("/chat/send", json=body).status_code == 422


def test_history_empty_without_thread():
    resp = client.get(f"/chat/history?session_id={GUEST_SESSION}")
    assert resp.status_code == 200
    assert resp.json() == []


def test_history_empty_without_identity():
    assert client.get("/chat/history").json() == []


def test_guest_history_in_order():
    for text in ("first", "second"):
        client.post("/chat/send", json={"message": text, "session_id": GUEST_SESSION})

    resp = client.get(f"/chat/history?session_id={GUEST_SESSION}")
    assert [m["message"] for m in resp.json()] == ["first", "second"]


def test_signed_in_history_includes_admin_replies(db):
    conv = _add_conversation(db, user_id=10)
    _add_message(db, conv.id, "question", sender_id=10)
    _add_message(db, conv.id, "answer", is_from_admin=True, sender_id=1)

    resp = client.get("/chat/history", headers=STUDENT)
    assert [m["message"] for m in resp.json()] == ["question", "answer"]


def test_guest_cannot_read_claimed_thread_by_session_id(db):
    conv = _add_conversation(db, user_id=10, session_id=GUEST_SESSION)
    _add_message(db, conv.id, "private", sender_id=10)

    resp = client.get(f"/chat/history?session_id={GUEST_SESSION}")
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Chat — sync
# ---------------------------------------------------------------------------


def test_sync_requires_sign_in():
    resp = client.post("/chat/sync", json={"session_id": GUEST_SESSION})
    assert resp.status_code == 401


def test_sync_rejects_bad_session_id():
    resp = client.post("/chat/sync", json={"session_id": "bad!"}, headers=STUDENT)
    assert resp.status_code == 422


def test_sync_nothing_to_sync():
    resp = client.post(
        "/chat/sync", json={"session_id": GUEST_SESSION}, headers=STUDENT
    )
    assert resp.status_code == 200
    assert resp.json() == {"message": "Nothing to sync"}


def test_sync_attaches_guest_thread(db):
    guest = _add_conversation(db, session_id=GUEST_SESSION)

    resp = client.post(
        "/chat/sync", json={"session_id": GUEST_SESSION}, headers=STUDENT
    )
    assert resp.json() == {"message": "Chat session synced"}

    db.refresh(guest)
    assert guest.user_id == 10
    assert guest.session_id is None


def test_sync_merges_guest_thread_into_existing_one(db):
    own = _add_conversation(db, user_id=10, last_message_at=_at(0))
    guest = _add_conversation(db, session_id=GUEST_SESSION, last_message_at=_at(10))
    _add_message(db, own.id, "older", sender_id=10)
    _add_message(db, guest.id, "as guest")
    guest_id = guest.id

    resp = client.post(
        "/chat/sync", json={"session_id": GUEST_SESSION}, headers=STUDENT
    )
    assert resp.json() == {"message": "Chat session synced"}

    db.expire_all()
    assert db.get(ChatConversation, guest_id) is None
    messages = db.query(ChatMessage).filter_by(conversation_id=own.id).all()
    assert {m.message for m in messages} == {"older", "as guest"}
    assert db.get(ChatConversation, own.id).last_message_at.replace(
        tzinfo=timezone.utc
    ) == _at(10)


def test_sync_ignores_thread_claimed_by_someone_else(db):
    claimed = _add_conversation(db, user_id=10, session_id=GUEST_SESSION)

    resp = client.post(
        "/chat/sync", json={"session_id": GUEST_SESSION}, headers=OTHER_STUDENT
    )
    assert resp.json() == {"message": "Nothing to sync"}
    db.refresh(claimed)
    assert claimed.user_id == 10


# ---------------------------------------------------------------------------
# Chat — admin side
# ---------------------------------------------------------------------------


def test_admin_chat_routes_require_admin(db):
    conv = _add_conversation(db, user_id=10)
    routes = [
        ("get", "/chat/admin/conversations"),
        ("get", "/chat/admin/conversations/student/10"),
        ("get", f"/chat/admin/conversations/{conv.id}/messages"),
        ("post", f"/chat/admin/conversations/{conv.id}/reply"),
        ("delete", f"/chat/admin/conversations/{conv.id}"),
    ]
    for method, url in routes:
        kwargs = {"json": {"message": "hi"}} if method == "post" else {}
        assert getattr(client, method)(url, **kwargs).status_code == 401, url
        assert (
            getattr(client, method)(url, headers=STUDENT, **kwargs).status_code == 403
        ), url


def test_admin_list_conversations_empty():
    resp = client.get("/chat/admin/conversations", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json() == []


def test_admin_list_conversations_summary(db):
    _add_replica_user(db, 10, full_name="Sunil Silva", email="sunil@example.com")
    named = _add_conversation(db, user_id=10, last_message_at=_at(5))
    guest = _add_conversation(db, session_id=GUEST_SESSION, last_message_at=_at(10))
    unknown = _add_conversation(db, user_id=99, last_message_at=_at(1))

    _add_message(db, named.id, "student 1", sender_id=10)
    _add_message(db, named.id, "student 2", sender_id=10)
    _add_message(db, named.id, "already seen", sender_id=10, is_read=True)
    _add_message(db, named.id, "admin answer", is_from_admin=True, sender_id=1)

    resp = client.get("/chat/admin/conversations", headers=ADMIN)
    assert resp.status_code == 200
    rows = resp.json()

    assert [r["id"] for r in rows] == [guest.id, named.id, unknown.id]
    by_id = {r["id"]: r for r in rows}

    assert by_id[named.id]["student_name"] == "Sunil Silva"
    # Admin replies and read messages do not count as unread.
    assert by_id[named.id]["unread_count"] == 2
    assert by_id[named.id]["last_message"] == "admin answer"

    assert by_id[guest.id]["student_name"] == "Guest"
    assert by_id[guest.id]["last_message"] is None
    assert by_id[guest.id]["unread_count"] == 0

    # Signed-in user with no replica yet.
    assert by_id[unknown.id]["student_name"] == "Student"


def test_admin_opens_existing_student_thread(db):
    conv = _add_conversation(db, user_id=10)

    resp = client.get("/chat/admin/conversations/student/10", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["id"] == conv.id


def test_admin_starts_thread_with_known_student(db):
    _add_replica_user(db, 10, email="sunil@example.com")

    resp = client.get("/chat/admin/conversations/student/10", headers=ADMIN)
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == 10
    assert data["student_name"] == "sunil@example.com"
    assert db.query(ChatConversation).filter_by(user_id=10).count() == 1


def test_admin_cannot_start_thread_with_unknown_user(db):
    resp = client.get("/chat/admin/conversations/student/404", headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "USER_NOT_FOUND"
    assert db.query(ChatConversation).count() == 0


def test_admin_reading_messages_marks_student_messages_read(db):
    conv = _add_conversation(db, user_id=10)
    _add_message(db, conv.id, "question", sender_id=10)
    reply = _add_message(db, conv.id, "answer", is_from_admin=True, sender_id=1)

    resp = client.get(f"/chat/admin/conversations/{conv.id}/messages", headers=ADMIN)
    assert resp.status_code == 200
    assert [m["message"] for m in resp.json()] == ["question", "answer"]
    assert resp.json()[0]["is_read"] is True

    # The student has not read the admin's reply; that flag is left alone.
    db.refresh(reply)
    assert reply.is_read is False

    summary = client.get("/chat/admin/conversations", headers=ADMIN).json()
    assert summary[0]["unread_count"] == 0


def test_admin_messages_not_found():
    resp = client.get("/chat/admin/conversations/999/messages", headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "CONVERSATION_NOT_FOUND"


def test_admin_reply(db):
    conv = _add_conversation(db, user_id=10, last_message_at=_at(0))

    resp = client.post(
        f"/chat/admin/conversations/{conv.id}/reply",
        json={"message": "  We can help  "},
        headers=ADMIN,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "We can help"
    assert data["is_from_admin"] is True
    assert data["sender_id"] == 1

    db.refresh(conv)
    assert conv.last_message_at.replace(tzinfo=timezone.utc) > _at(0)

    history = client.get("/chat/history", headers=STUDENT).json()
    assert [m["message"] for m in history] == ["We can help"]


def test_admin_reply_not_found():
    resp = client.post(
        "/chat/admin/conversations/999/reply", json={"message": "hi"}, headers=ADMIN
    )
    assert resp.status_code == 404


@pytest.mark.parametrize("message", ["", "x" * 4001])
def test_admin_reply_validation(db, message):
    conv = _add_conversation(db, user_id=10)
    resp = client.post(
        f"/chat/admin/conversations/{conv.id}/reply",
        json={"message": message},
        headers=ADMIN,
    )
    assert resp.status_code == 422


def test_admin_delete_conversation(db):
    conv = _add_conversation(db, user_id=10)
    other = _add_conversation(db, user_id=11)
    _add_message(db, conv.id, "bye", sender_id=10)
    _add_message(db, other.id, "keep me", sender_id=11)
    conv_id = conv.id

    resp = client.delete(f"/chat/admin/conversations/{conv_id}", headers=ADMIN)
    assert resp.status_code == 204

    db.expire_all()
    assert db.get(ChatConversation, conv_id) is None
    assert db.query(ChatMessage).filter_by(conversation_id=conv_id).count() == 0
    assert db.query(ChatMessage).filter_by(conversation_id=other.id).count() == 1


def test_admin_delete_conversation_not_found():
    resp = client.delete("/chat/admin/conversations/999", headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "CONVERSATION_NOT_FOUND"


# ---------------------------------------------------------------------------
# people.display_names
# ---------------------------------------------------------------------------


async def _display_names(ids):
    return await people.display_names(ids)


def test_display_names_skips_db_for_no_ids(monkeypatch):
    def fail(*_):
        raise AssertionError("should not query")

    monkeypatch.setattr(people, "_names", fail)

    assert asyncio.run(_display_names([])) == {}
    assert asyncio.run(_display_names([None])) == {}


def test_display_names_prefers_full_name_then_email(db):
    _add_replica_user(db, 1, full_name="Full Name", email="full@example.com")
    _add_replica_user(db, 2, email="email@example.com")
    _add_replica_user(db, 3)

    names = asyncio.run(_display_names([1, 2, 3, 2, None, 404]))
    # No name or email, or no replica at all: left out.
    assert names == {1: "Full Name", 2: "email@example.com"}


# ---------------------------------------------------------------------------
# event_consumer
# ---------------------------------------------------------------------------


def test_consumer_stores_profile(db):
    event_consumer.handle(
        ev.USER_PROFILE_UPSERTED,
        {"user_id": 10, "first_name": "Sunil", "last_name": "Silva", "role": "student"},
    )

    user = db.get(ReplicaUser, 10)
    assert user.full_name == "Sunil Silva"
    assert user.display_name == "Sunil Silva"


def test_consumer_stores_account_then_profile(db):
    event_consumer.handle(
        ev.AUTH_USER_REGISTERED,
        {"user_id": 10, "email": "sunil@example.com", "role": "student"},
    )
    assert db.get(ReplicaUser, 10).display_name == "sunil@example.com"

    event_consumer.handle(
        ev.USER_PROFILE_UPSERTED, {"user_id": 10, "full_name": "Sunil Silva"}
    )
    db.expire_all()
    user = db.get(ReplicaUser, 10)
    assert user.email == "sunil@example.com"
    assert user.display_name == "Sunil Silva"


def test_consumer_deletes_profile(db):
    _add_replica_user(db, 10, full_name="Sunil Silva")

    event_consumer.handle(ev.USER_PROFILE_DELETED, {"user_id": 10})

    db.expire_all()
    assert db.get(ReplicaUser, 10) is None


def test_consumer_ignores_unrelated_events(db):
    event_consumer.handle("course.upserted", {"id": 1, "title": "Maths"})
    assert db.query(ReplicaUser).count() == 0


def test_consumer_rolls_back_and_reraises(monkeypatch):
    class FakeSession:
        rolled_back = closed = False

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    session = FakeSession()

    def explode(*_, **__):
        raise ValueError("bad event")

    monkeypatch.setattr(event_consumer, "SessionLocal", lambda: session)
    monkeypatch.setattr(event_consumer.replicas, "apply", explode)

    with pytest.raises(ValueError):
        event_consumer.handle(ev.USER_PROFILE_UPSERTED, {"user_id": 1})
    assert session.rolled_back
    assert session.closed


def test_consumer_start_binds_admin_queue(monkeypatch):
    calls = []
    monkeypatch.setattr(
        event_consumer.messaging,
        "start_consumer",
        lambda *args: calls.append(args),
    )

    event_consumer.start()

    assert calls == [
        ("admin", "admin.queue", ev.QUEUES["admin.queue"], event_consumer.handle)
    ]
    assert set(ev.QUEUES["admin.queue"]) == {
        ev.AUTH_USER_REGISTERED,
        ev.AUTH_ACCOUNT_UPDATED,
        ev.USER_PROFILE_UPSERTED,
        ev.USER_PROFILE_DELETED,
    }
