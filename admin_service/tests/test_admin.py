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


def test_given_running_service_when_health_requested_then_200():
    # Act
    resp = client.get("/health")

    # Assert
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "admin_service"}


def test_given_service_starting_when_startup_runs_then_tables_created_and_consumer_started(
    monkeypatch,
):
    # Arrange
    calls = []
    Base.metadata.drop_all(bind=engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("start"))

    main.on_startup()

    # Act and assert
    assert calls == ["start"]
    assert TestingSessionLocal().query(Feedback).count() == 0


def test_given_database_unavailable_when_startup_runs_then_consumer_still_starts(
    monkeypatch,
):
    # Arrange
    calls = []

    def broken_create_all(bind):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken_create_all)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("start"))

    # Act
    main.on_startup()

    # Assert
    assert calls == ["start"]


# ---------------------------------------------------------------------------
# /admin and role checks
# ---------------------------------------------------------------------------


def test_given_admin_token_when_admin_endpoint_called_then_200():
    # Act
    resp = client.get("/admin", headers=ADMIN)

    # Assert
    assert resp.status_code == 200
    assert resp.json() == {"message": "Welcome Admin"}


def test_given_uppercase_admin_role_when_admin_endpoint_called_then_200():
    # Act
    resp = client.get("/admin", headers=_auth(1, "ADMIN"))

    # Assert
    assert resp.status_code == 200


@pytest.mark.parametrize("role", ["student", "teacher"])
def test_given_non_admin_token_when_admin_endpoint_called_then_403_insufficient_permissions(
    role,
):
    # Act
    resp = client.get("/admin", headers=_auth(5, role))

    # Assert
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


def test_given_no_token_when_admin_endpoint_called_then_401_missing_token():
    # Act
    resp = client.get("/admin")

    # Assert
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "MISSING_TOKEN"


def test_given_expired_token_when_admin_endpoint_called_then_401_token_expired():
    # Arrange
    expired = _auth(1, "admin", exp=datetime.now(timezone.utc) - timedelta(minutes=1))

    # Act
    resp = client.get("/admin", headers=expired)

    # Assert
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "TOKEN_EXPIRED"


def test_given_bad_signature_when_admin_endpoint_called_then_401_invalid_token():
    # Arrange
    token = jwt.encode({"sub": "1", "role": "admin"}, "wrong-key", algorithm=ALGORITHM)

    # Act
    resp = client.get("/admin", headers={"Authorization": f"Bearer {token}"})

    # Assert
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "INVALID_TOKEN"


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


def test_given_guest_when_submitting_feedback_then_201():
    # Act
    resp = client.post("/feedback/submit", json=_feedback_body())

    # Assert
    assert resp.status_code == 201

    # Act
    data = resp.json()

    # Assert
    assert data["user_id"] is None
    assert data["name"] == "Nimal Perera"
    assert data["category"] == "suggestion"
    assert data["is_read"] is False
    assert data["is_resolved"] is False


def test_given_signed_in_user_when_submitting_feedback_then_it_is_linked_to_the_user():
    # Act
    resp = client.post("/feedback/submit", json=_feedback_body(), headers=STUDENT)

    # Assert
    assert resp.status_code == 201
    assert resp.json()["user_id"] == 10


def test_given_invalid_token_when_submitting_feedback_then_it_is_saved_as_guest():
    # Act
    resp = client.post(
        "/feedback/submit",
        json=_feedback_body(),
        headers={"Authorization": "Bearer not-a-jwt"},
    )

    # Assert
    assert resp.status_code == 201
    assert resp.json()["user_id"] is None


def test_given_padded_text_and_blank_subject_when_submitting_feedback_then_text_is_trimmed_and_subject_dropped():
    # Arrange
    resp = client.post(
        "/feedback/submit",
        json=_feedback_body(name="  Kamal  ", subject="   ", message="  Hi  "),
    )

    # Act
    data = resp.json()

    # Assert
    assert data["name"] == "Kamal"
    assert data["subject"] is None
    assert data["message"] == "Hi"


def test_given_no_subject_when_submitting_feedback_then_201():
    # Arrange
    body = _feedback_body()
    del body["subject"]

    # Act
    resp = client.post("/feedback/submit", json=body)

    # Assert
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
def test_given_invalid_feedback_when_submitted_then_422(overrides):
    # Act
    resp = client.post("/feedback/submit", json=_feedback_body(**overrides))

    # Assert
    assert resp.status_code == 422


def test_given_feedback_when_admin_lists_it_then_newest_first(db):
    # Arrange
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

    # Act
    resp = client.get("/feedback/admin/all", headers=ADMIN)

    # Assert
    assert resp.status_code == 200
    assert [f["name"] for f in resp.json()] == ["New", "Old"]


def test_given_skip_and_limit_when_admin_lists_feedback_then_page_is_returned():
    # Arrange
    for i in range(3):
        client.post("/feedback/submit", json=_feedback_body(name=f"User {i}"))

    # Act
    resp = client.get("/feedback/admin/all?skip=1&limit=1", headers=ADMIN)

    # Assert
    assert resp.status_code == 200
    # Same created_at second in SQLite, so the id tiebreak decides: 3, 2, 1.
    assert [f["name"] for f in resp.json()] == ["User 1"]


@pytest.mark.parametrize("query", ["skip=-1", "limit=0", "limit=501"])
def test_given_bad_paging_values_when_admin_lists_feedback_then_422(query):
    # Act
    resp = client.get(f"/feedback/admin/all?{query}", headers=ADMIN)

    # Assert
    assert resp.status_code == 422


def test_given_no_token_or_non_admin_when_listing_feedback_then_401_or_403():
    # Act and assert
    assert client.get("/feedback/admin/all").status_code == 401
    assert client.get("/feedback/admin/all", headers=STUDENT).status_code == 403


def test_given_feedback_when_admin_marks_it_read_then_it_is_marked_read():
    # Arrange
    fid = client.post("/feedback/submit", json=_feedback_body()).json()["id"]

    # Act
    resp = client.patch(f"/feedback/admin/{fid}/read", headers=ADMIN)

    # Assert
    assert resp.status_code == 200
    assert resp.json()["is_read"] is True


def test_given_unknown_feedback_when_marked_read_then_404_feedback_not_found():
    # Act
    resp = client.patch("/feedback/admin/999/read", headers=ADMIN)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "FEEDBACK_NOT_FOUND"


def test_given_non_admin_when_marking_feedback_read_then_403():
    # Arrange
    fid = client.post("/feedback/submit", json=_feedback_body()).json()["id"]

    # Act
    resp = client.patch(f"/feedback/admin/{fid}/read", headers=STUDENT)

    # Assert
    assert resp.status_code == 403


def test_given_feedback_when_admin_deletes_it_then_204_and_it_is_removed(db):
    # Arrange
    fid = client.post("/feedback/submit", json=_feedback_body()).json()["id"]

    # Act
    resp = client.delete(f"/feedback/admin/{fid}", headers=ADMIN)

    # Assert
    assert resp.status_code == 204

    # Act and assert
    assert db.get(Feedback, fid) is None


def test_given_unknown_feedback_when_deleted_then_404_feedback_not_found():
    # Act
    resp = client.delete("/feedback/admin/999", headers=ADMIN)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "FEEDBACK_NOT_FOUND"


def test_given_non_admin_when_deleting_feedback_then_403_and_it_is_kept(db):
    # Arrange
    fid = client.post("/feedback/submit", json=_feedback_body()).json()["id"]

    # Act
    resp = client.delete(f"/feedback/admin/{fid}", headers=STUDENT)

    # Assert
    assert resp.status_code == 403

    # Act and assert
    assert db.get(Feedback, fid) is not None


# ---------------------------------------------------------------------------
# Chat — student / guest side
# ---------------------------------------------------------------------------


def test_given_no_token_and_no_session_id_when_sending_chat_message_then_400_identity_required():
    # Act
    resp = client.post("/chat/send", json={"message": "hello"})

    # Assert
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "IDENTITY_REQUIRED"


def test_given_guest_session_when_sending_messages_then_a_thread_is_started_and_reused(
    db,
):
    # Act
    first = client.post(
        "/chat/send", json={"message": "  hello  ", "session_id": GUEST_SESSION}
    )

    # Assert
    assert first.status_code == 200

    # Act
    data = first.json()

    # Assert
    assert data["message"] == "hello"
    assert data["sender_id"] is None
    assert data["is_from_admin"] is False

    # Act
    second = client.post(
        "/chat/send", json={"message": "again", "session_id": GUEST_SESSION}
    )

    # Assert
    assert second.json()["conversation_id"] == data["conversation_id"]

    # Act
    conv = db.get(ChatConversation, data["conversation_id"])

    # Assert
    assert conv.session_id == GUEST_SESSION
    assert conv.user_id is None


def test_given_signed_in_user_when_sending_message_then_a_user_thread_is_started(db):
    # Act
    resp = client.post(
        "/chat/send",
        json={"message": "hi", "session_id": GUEST_SESSION},
        headers=STUDENT,
    )

    # Assert
    assert resp.status_code == 200
    assert resp.json()["sender_id"] == 10

    # Act
    conv = db.get(ChatConversation, resp.json()["conversation_id"])

    # Assert
    assert conv.user_id == 10
    # A signed-in thread is keyed by the user, not the browser session.
    assert conv.session_id is None


def test_given_signed_in_user_with_thread_when_sending_again_then_own_thread_is_reused():
    # Arrange
    first = client.post("/chat/send", json={"message": "one"}, headers=STUDENT)

    # Act
    second = client.post("/chat/send", json={"message": "two"}, headers=STUDENT)

    # Assert
    assert first.json()["conversation_id"] == second.json()["conversation_id"]


def test_given_unclaimed_guest_thread_when_signed_in_user_sends_with_its_session_then_thread_is_continued(
    db,
):
    # Arrange
    guest = _add_conversation(db, session_id=GUEST_SESSION)

    # Act
    resp = client.post(
        "/chat/send",
        json={"message": "signed in now", "session_id": GUEST_SESSION},
        headers=STUDENT,
    )

    # Assert
    assert resp.json()["conversation_id"] == guest.id


def test_given_thread_of_another_user_when_signed_in_user_sends_with_its_session_then_it_is_not_used(
    db,
):
    # Arrange
    claimed = _add_conversation(db, user_id=10, session_id=GUEST_SESSION)

    # Act
    resp = client.post(
        "/chat/send",
        json={"message": "hijack", "session_id": GUEST_SESSION},
        headers=OTHER_STUDENT,
    )

    # Assert
    assert resp.status_code == 200
    assert resp.json()["conversation_id"] != claimed.id


def test_given_thread_when_message_sent_then_last_message_time_is_updated(db):
    # Arrange
    conv = _add_conversation(db, session_id=GUEST_SESSION, last_message_at=_at(0))

    client.post("/chat/send", json={"message": "hi", "session_id": GUEST_SESSION})

    db.refresh(conv)

    # Act and assert
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
def test_given_invalid_message_when_sent_then_422(body):
    # Act and assert
    assert client.post("/chat/send", json=body).status_code == 422


def test_given_no_thread_when_history_requested_then_empty_list_returned():
    # Act
    resp = client.get(f"/chat/history?session_id={GUEST_SESSION}")

    # Assert
    assert resp.status_code == 200
    assert resp.json() == []


def test_given_no_identity_when_history_requested_then_empty_list_returned():
    # Act and assert
    assert client.get("/chat/history").json() == []


def test_given_guest_messages_when_history_requested_then_messages_are_in_order():
    # Arrange
    for text in ("first", "second"):
        client.post("/chat/send", json={"message": text, "session_id": GUEST_SESSION})

    # Act
    resp = client.get(f"/chat/history?session_id={GUEST_SESSION}")

    # Assert
    assert [m["message"] for m in resp.json()] == ["first", "second"]


def test_given_admin_reply_when_signed_in_user_requests_history_then_reply_is_included(
    db,
):
    # Arrange
    conv = _add_conversation(db, user_id=10)
    _add_message(db, conv.id, "question", sender_id=10)
    _add_message(db, conv.id, "answer", is_from_admin=True, sender_id=1)

    # Act
    resp = client.get("/chat/history", headers=STUDENT)

    # Assert
    assert [m["message"] for m in resp.json()] == ["question", "answer"]


def test_given_claimed_thread_when_guest_requests_history_with_its_session_id_then_it_is_not_returned(
    db,
):
    # Arrange
    conv = _add_conversation(db, user_id=10, session_id=GUEST_SESSION)
    _add_message(db, conv.id, "private", sender_id=10)

    # Act
    resp = client.get(f"/chat/history?session_id={GUEST_SESSION}")

    # Assert
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Chat — sync
# ---------------------------------------------------------------------------


def test_given_no_token_when_syncing_session_then_401():
    # Act
    resp = client.post("/chat/sync", json={"session_id": GUEST_SESSION})

    # Assert
    assert resp.status_code == 401


def test_given_bad_session_id_when_syncing_then_422():
    # Act
    resp = client.post("/chat/sync", json={"session_id": "bad!"}, headers=STUDENT)

    # Assert
    assert resp.status_code == 422


def test_given_no_guest_thread_when_syncing_then_nothing_is_synced():
    # Act
    resp = client.post(
        "/chat/sync", json={"session_id": GUEST_SESSION}, headers=STUDENT
    )

    # Assert
    assert resp.status_code == 200
    assert resp.json() == {"message": "Nothing to sync"}


def test_given_guest_thread_when_user_syncs_then_thread_is_attached_to_the_user(db):
    # Arrange
    guest = _add_conversation(db, session_id=GUEST_SESSION)

    # Act
    resp = client.post(
        "/chat/sync", json={"session_id": GUEST_SESSION}, headers=STUDENT
    )

    # Assert
    assert resp.json() == {"message": "Chat session synced"}

    db.refresh(guest)

    # Act and assert
    assert guest.user_id == 10
    assert guest.session_id is None


def test_given_user_with_thread_when_syncing_guest_thread_then_messages_are_merged_into_it(
    db,
):
    # Arrange
    own = _add_conversation(db, user_id=10, last_message_at=_at(0))
    guest = _add_conversation(db, session_id=GUEST_SESSION, last_message_at=_at(10))
    _add_message(db, own.id, "older", sender_id=10)
    _add_message(db, guest.id, "as guest")
    guest_id = guest.id

    # Act
    resp = client.post(
        "/chat/sync", json={"session_id": GUEST_SESSION}, headers=STUDENT
    )

    # Assert
    assert resp.json() == {"message": "Chat session synced"}

    db.expire_all()

    # Act and assert
    assert db.get(ChatConversation, guest_id) is None

    # Act
    messages = db.query(ChatMessage).filter_by(conversation_id=own.id).all()

    # Assert
    assert {m.message for m in messages} == {"older", "as guest"}

    # Act and assert
    assert db.get(ChatConversation, own.id).last_message_at.replace(
        tzinfo=timezone.utc
    ) == _at(10)


def test_given_thread_claimed_by_someone_else_when_syncing_then_it_is_ignored(db):
    # Arrange
    claimed = _add_conversation(db, user_id=10, session_id=GUEST_SESSION)

    # Act
    resp = client.post(
        "/chat/sync", json={"session_id": GUEST_SESSION}, headers=OTHER_STUDENT
    )

    # Assert
    assert resp.json() == {"message": "Nothing to sync"}
    db.refresh(claimed)

    # Act and assert
    assert claimed.user_id == 10


# ---------------------------------------------------------------------------
# Chat — admin side
# ---------------------------------------------------------------------------


def test_given_no_token_or_non_admin_when_calling_admin_chat_routes_then_401_or_403(db):
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


def test_given_no_conversations_when_admin_lists_them_then_empty_list_returned():
    # Act
    resp = client.get("/chat/admin/conversations", headers=ADMIN)

    # Assert
    assert resp.status_code == 200
    assert resp.json() == []


def test_given_conversations_when_admin_lists_them_then_summaries_are_returned(db):
    # Arrange
    _add_replica_user(db, 10, full_name="Sunil Silva", email="sunil@example.com")
    named = _add_conversation(db, user_id=10, last_message_at=_at(5))
    guest = _add_conversation(db, session_id=GUEST_SESSION, last_message_at=_at(10))
    unknown = _add_conversation(db, user_id=99, last_message_at=_at(1))

    _add_message(db, named.id, "student 1", sender_id=10)
    _add_message(db, named.id, "student 2", sender_id=10)
    _add_message(db, named.id, "already seen", sender_id=10, is_read=True)
    _add_message(db, named.id, "admin answer", is_from_admin=True, sender_id=1)

    # Act
    resp = client.get("/chat/admin/conversations", headers=ADMIN)

    # Assert
    assert resp.status_code == 200

    # Act
    rows = resp.json()

    # Assert
    assert [r["id"] for r in rows] == [guest.id, named.id, unknown.id]
    by_id = {r["id"]: r for r in rows}

    # Act and assert
    assert by_id[named.id]["student_name"] == "Sunil Silva"
    # Admin replies and read messages do not count as unread.
    assert by_id[named.id]["unread_count"] == 2
    assert by_id[named.id]["last_message"] == "admin answer"

    assert by_id[guest.id]["student_name"] == "Guest"
    assert by_id[guest.id]["last_message"] is None
    assert by_id[guest.id]["unread_count"] == 0

    # Signed-in user with no replica yet.
    assert by_id[unknown.id]["student_name"] == "Student"


def test_given_existing_student_thread_when_admin_opens_it_then_thread_is_returned(db):
    # Arrange
    conv = _add_conversation(db, user_id=10)

    # Act
    resp = client.get("/chat/admin/conversations/student/10", headers=ADMIN)

    # Assert
    assert resp.status_code == 200
    assert resp.json()["id"] == conv.id


def test_given_known_student_without_thread_when_admin_opens_conversation_then_thread_is_started(
    db,
):
    # Arrange
    _add_replica_user(db, 10, email="sunil@example.com")

    # Act
    resp = client.get("/chat/admin/conversations/student/10", headers=ADMIN)

    # Assert
    assert resp.status_code == 200

    data = resp.json()

    # Act and assert
    assert data["user_id"] == 10
    assert data["student_name"] == "sunil@example.com"
    assert db.query(ChatConversation).filter_by(user_id=10).count() == 1


def test_given_unknown_user_when_admin_opens_conversation_then_404_user_not_found(db):
    # Act
    resp = client.get("/chat/admin/conversations/student/404", headers=ADMIN)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "USER_NOT_FOUND"
    assert db.query(ChatConversation).count() == 0


def test_given_unread_student_messages_when_admin_reads_them_then_they_are_marked_read(
    db,
):
    # Arrange
    conv = _add_conversation(db, user_id=10)
    _add_message(db, conv.id, "question", sender_id=10)
    reply = _add_message(db, conv.id, "answer", is_from_admin=True, sender_id=1)

    # Act
    resp = client.get(f"/chat/admin/conversations/{conv.id}/messages", headers=ADMIN)

    # Assert
    assert resp.status_code == 200
    assert [m["message"] for m in resp.json()] == ["question", "answer"]
    assert resp.json()[0]["is_read"] is True

    # The student has not read the admin's reply; that flag is left alone.
    db.refresh(reply)

    # Act and assert
    assert reply.is_read is False

    # Act
    summary = client.get("/chat/admin/conversations", headers=ADMIN).json()

    # Assert
    assert summary[0]["unread_count"] == 0


def test_given_unknown_conversation_when_admin_reads_messages_then_404_conversation_not_found():
    # Act
    resp = client.get("/chat/admin/conversations/999/messages", headers=ADMIN)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "CONVERSATION_NOT_FOUND"


def test_given_conversation_when_admin_replies_then_reply_is_saved_and_visible_in_history(
    db,
):
    # Arrange
    conv = _add_conversation(db, user_id=10, last_message_at=_at(0))

    # Act
    resp = client.post(
        f"/chat/admin/conversations/{conv.id}/reply",
        json={"message": "  We can help  "},
        headers=ADMIN,
    )

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()

    # Assert
    assert data["message"] == "We can help"
    assert data["is_from_admin"] is True
    assert data["sender_id"] == 1

    db.refresh(conv)

    # Act and assert
    assert conv.last_message_at.replace(tzinfo=timezone.utc) > _at(0)

    # Act
    history = client.get("/chat/history", headers=STUDENT).json()

    # Assert
    assert [m["message"] for m in history] == ["We can help"]


def test_given_unknown_conversation_when_admin_replies_then_404():
    # Act
    resp = client.post(
        "/chat/admin/conversations/999/reply", json={"message": "hi"}, headers=ADMIN
    )

    # Assert
    assert resp.status_code == 404


@pytest.mark.parametrize("message", ["", "x" * 4001])
def test_given_invalid_reply_when_admin_replies_then_422(db, message):
    # Arrange
    conv = _add_conversation(db, user_id=10)

    # Act
    resp = client.post(
        f"/chat/admin/conversations/{conv.id}/reply",
        json={"message": message},
        headers=ADMIN,
    )

    # Assert
    assert resp.status_code == 422


def test_given_conversation_when_admin_deletes_it_then_204_and_it_is_removed(db):
    # Arrange
    conv = _add_conversation(db, user_id=10)
    other = _add_conversation(db, user_id=11)
    _add_message(db, conv.id, "bye", sender_id=10)
    _add_message(db, other.id, "keep me", sender_id=11)
    conv_id = conv.id

    # Act
    resp = client.delete(f"/chat/admin/conversations/{conv_id}", headers=ADMIN)

    # Assert
    assert resp.status_code == 204

    db.expire_all()

    # Act and assert
    assert db.get(ChatConversation, conv_id) is None

    # Act and assert
    assert db.query(ChatMessage).filter_by(conversation_id=conv_id).count() == 0
    assert db.query(ChatMessage).filter_by(conversation_id=other.id).count() == 1


def test_given_unknown_conversation_when_admin_deletes_it_then_404_conversation_not_found():
    # Act
    resp = client.delete("/chat/admin/conversations/999", headers=ADMIN)

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "CONVERSATION_NOT_FOUND"


# ---------------------------------------------------------------------------
# people.display_names
# ---------------------------------------------------------------------------


async def _display_names(ids):
    return await people.display_names(ids)


def test_given_no_ids_when_display_names_requested_then_database_is_not_queried(
    monkeypatch,
):
    # Arrange
    def fail(*_):
        raise AssertionError("should not query")

    monkeypatch.setattr(people, "_names", fail)

    # Act and assert
    assert asyncio.run(_display_names([])) == {}
    assert asyncio.run(_display_names([None])) == {}


def test_given_users_when_display_names_requested_then_full_name_is_preferred_over_email(
    db,
):
    # Arrange
    _add_replica_user(db, 1, full_name="Full Name", email="full@example.com")
    _add_replica_user(db, 2, email="email@example.com")
    _add_replica_user(db, 3)

    # Act
    names = asyncio.run(_display_names([1, 2, 3, 2, None, 404]))
    # No name or email, or no replica at all: left out.
    # Assert
    assert names == {1: "Full Name", 2: "email@example.com"}


# ---------------------------------------------------------------------------
# event_consumer
# ---------------------------------------------------------------------------


def test_given_profile_event_when_consumed_then_profile_is_stored(db):
    # Arrange
    event_consumer.handle(
        ev.USER_PROFILE_UPSERTED,
        {"user_id": 10, "first_name": "Sunil", "last_name": "Silva", "role": "student"},
    )

    # Act
    user = db.get(ReplicaUser, 10)

    # Assert
    assert user.full_name == "Sunil Silva"
    assert user.display_name == "Sunil Silva"


def test_given_account_then_profile_events_when_consumed_then_both_are_stored(db):
    # Arrange
    event_consumer.handle(
        ev.AUTH_USER_REGISTERED,
        {"user_id": 10, "email": "sunil@example.com", "role": "student"},
    )

    # Act and assert
    assert db.get(ReplicaUser, 10).display_name == "sunil@example.com"

    event_consumer.handle(
        ev.USER_PROFILE_UPSERTED, {"user_id": 10, "full_name": "Sunil Silva"}
    )
    db.expire_all()

    # Act
    user = db.get(ReplicaUser, 10)

    # Assert
    assert user.email == "sunil@example.com"
    assert user.display_name == "Sunil Silva"


def test_given_profile_deleted_event_when_consumed_then_profile_is_removed(db):
    # Arrange
    _add_replica_user(db, 10, full_name="Sunil Silva")

    event_consumer.handle(ev.USER_PROFILE_DELETED, {"user_id": 10})

    db.expire_all()

    # Act and assert
    assert db.get(ReplicaUser, 10) is None


def test_given_unrelated_event_when_consumed_then_it_is_ignored(db):
    # Arrange
    event_consumer.handle("course.upserted", {"id": 1, "title": "Maths"})

    # Act and assert
    assert db.query(ReplicaUser).count() == 0


def test_given_handler_error_when_event_processed_then_session_rolled_back_and_error_reraised(
    monkeypatch,
):
    # Arrange
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

    # Act and assert
    with pytest.raises(ValueError):
        event_consumer.handle(ev.USER_PROFILE_UPSERTED, {"user_id": 1})

    # Act and assert
    assert session.rolled_back
    assert session.closed


def test_given_consumer_when_started_then_binds_admin_queue(monkeypatch):
    # Arrange
    calls = []
    monkeypatch.setattr(
        event_consumer.messaging,
        "start_consumer",
        lambda *args: calls.append(args),
    )

    # Act
    event_consumer.start()

    # Assert
    assert calls == [
        ("admin", "admin.queue", ev.QUEUES["admin.queue"], event_consumer.handle)
    ]
    assert set(ev.QUEUES["admin.queue"]) == {
        ev.AUTH_USER_REGISTERED,
        ev.AUTH_ACCOUNT_UPDATED,
        ev.USER_PROFILE_UPSERTED,
        ev.USER_PROFILE_DELETED,
    }
