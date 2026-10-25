"""Support chat: guest and student threads, signing in (sync), and the admin inbox."""

import pytest
from conftest import ADMIN, OTHER_STUDENT, STUDENT
from models import ChatConversation, ChatMessage

GUEST_SESSION = "guest-session-0001"
OTHER_SESSION = "guest-session-0002"


def send(client, message="Hello", session_id=None, headers=None):
    body = {"message": message}
    if session_id is not None:
        body["session_id"] = session_id
    return client.post("/chat/send", json=body, headers=headers or {})


def history(client, session_id=None, headers=None):
    params = {"session_id": session_id} if session_id else {}
    return client.get("/chat/history", params=params, headers=headers or {}).json()


def inbox(client):
    return client.get("/chat/admin/conversations", headers=ADMIN).json()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def test_guest_without_session_id_is_rejected(client):
    response = send(client)
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "IDENTITY_REQUIRED"


def test_guest_message_starts_a_thread_keyed_by_session(client, db):
    response = send(client, "  Hi, I need help  ", session_id=GUEST_SESSION)
    assert response.status_code == 200
    msg = response.json()
    assert msg["message"] == "Hi, I need help"
    assert msg["is_from_admin"] is False
    assert msg["is_read"] is False
    assert msg["sender_id"] is None
    conv = db.get(ChatConversation, msg["conversation_id"])
    assert conv.session_id == GUEST_SESSION
    assert conv.user_id is None


def test_guest_messages_share_one_thread(client):
    first = send(client, "one", session_id=GUEST_SESSION).json()
    second = send(client, "two", session_id=GUEST_SESSION).json()
    assert first["conversation_id"] == second["conversation_id"]


def test_different_guests_get_different_threads(client):
    a = send(client, session_id=GUEST_SESSION).json()
    b = send(client, session_id=OTHER_SESSION).json()
    assert a["conversation_id"] != b["conversation_id"]


def test_student_message_starts_a_thread_keyed_by_user(client, db):
    msg = send(client, headers=STUDENT).json()
    assert msg["sender_id"] == 7
    conv = db.get(ChatConversation, msg["conversation_id"])
    assert conv.user_id == 7
    assert conv.session_id is None


def test_student_keeps_their_own_thread_even_when_sending_a_session_id(client):
    own = send(client, headers=STUDENT).json()
    send(client, session_id=GUEST_SESSION)
    again = send(client, session_id=GUEST_SESSION, headers=STUDENT).json()
    assert again["conversation_id"] == own["conversation_id"]


def test_student_cannot_write_into_a_thread_claimed_by_someone_else(client, db):
    db.add(ChatConversation(session_id=GUEST_SESSION, user_id=8))
    db.commit()
    claimed_id = db.query(ChatConversation).one().id

    msg = send(client, session_id=GUEST_SESSION, headers=STUDENT).json()

    assert msg["conversation_id"] != claimed_id
    assert db.query(ChatMessage).filter_by(conversation_id=claimed_id).count() == 0


def test_invalid_token_falls_back_to_guest(client):
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert send(client, headers=bad).status_code == 400
    ok = send(client, session_id=GUEST_SESSION, headers=bad)
    assert ok.status_code == 200
    assert ok.json()["sender_id"] is None


@pytest.mark.parametrize(
    "session_id",
    ["short", "x" * 65, "has spaces in it", "semi;colon-0001"],
    ids=["too-short", "too-long", "spaces", "symbols"],
)
def test_malformed_session_ids_are_rejected(client, session_id):
    assert send(client, session_id=session_id).status_code == 422


@pytest.mark.parametrize("message", ["", "x" * 4001], ids=["empty", "too-long"])
def test_message_length_is_enforced(client, message):
    assert send(client, message, session_id=GUEST_SESSION).status_code == 422


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_history_is_empty_without_identity_or_thread(client):
    assert history(client) == []
    assert history(client, session_id=GUEST_SESSION) == []
    assert history(client, headers=STUDENT) == []


def test_guest_history_is_in_send_order(client):
    for text in ("one", "two", "three"):
        send(client, text, session_id=GUEST_SESSION)
    messages = history(client, session_id=GUEST_SESSION)
    assert [m["message"] for m in messages] == ["one", "two", "three"]


def test_history_only_shows_your_own_thread(client):
    send(client, "mine", headers=STUDENT)
    send(client, "theirs", headers=OTHER_STUDENT)
    send(client, "guest", session_id=GUEST_SESSION)
    assert [m["message"] for m in history(client, headers=STUDENT)] == ["mine"]
    assert [m["message"] for m in history(client, headers=OTHER_STUDENT)] == ["theirs"]


# ---------------------------------------------------------------------------
# Sync (guest signs in)
# ---------------------------------------------------------------------------


def sync(client, session_id=GUEST_SESSION, headers=STUDENT):
    return client.post("/chat/sync", json={"session_id": session_id}, headers=headers)


def test_sync_requires_sign_in(client):
    assert sync(client, headers={}).status_code == 401


def test_sync_rejects_a_malformed_session_id(client):
    assert sync(client, session_id="bad").status_code == 422


def test_sync_with_no_guest_thread_does_nothing(client):
    response = sync(client)
    assert response.status_code == 200
    assert response.json() == {"message": "Nothing to sync"}


def test_sync_hands_the_guest_thread_to_the_user(client, db):
    send(client, "before sign-in", session_id=GUEST_SESSION)

    assert sync(client).json() == {"message": "Chat session synced"}

    conv = db.query(ChatConversation).one()
    assert conv.user_id == 7
    assert conv.session_id is None
    assert [m["message"] for m in history(client, headers=STUDENT)] == [
        "before sign-in"
    ]
    # The browser's old session id no longer opens the thread.
    assert history(client, session_id=GUEST_SESSION) == []


def test_sync_merges_into_an_existing_user_thread(client, db):
    own = send(client, "as student", headers=STUDENT).json()
    send(client, "as guest", session_id=GUEST_SESSION)
    guest_last = (
        db.query(ChatConversation).filter_by(session_id=GUEST_SESSION).one()
    ).last_message_at

    assert sync(client).status_code == 200

    db.expire_all()
    conversations = db.query(ChatConversation).all()
    assert [c.id for c in conversations] == [own["conversation_id"]]
    assert conversations[0].last_message_at == guest_last
    assert [m["message"] for m in history(client, headers=STUDENT)] == [
        "as student",
        "as guest",
    ]


def test_a_claimed_thread_cannot_be_synced_again(client):
    send(client, "guest", session_id=GUEST_SESSION)
    sync(client)
    response = sync(client, headers=OTHER_STUDENT)
    assert response.json() == {"message": "Nothing to sync"}
    assert history(client, headers=OTHER_STUDENT) == []


@pytest.mark.parametrize("role", ["student", "teacher", "admin"])
def test_any_signed_in_role_can_sync(client, role):
    from conftest import auth

    assert sync(client, headers=auth(role, 20)).status_code == 200


# ---------------------------------------------------------------------------
# Admin inbox
# ---------------------------------------------------------------------------

ADMIN_ROUTES = [
    ("get", "/chat/admin/conversations"),
    ("get", "/chat/admin/conversations/student/7"),
    ("get", "/chat/admin/conversations/1/messages"),
    ("post", "/chat/admin/conversations/1/reply"),
    ("delete", "/chat/admin/conversations/1"),
]


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_admin_routes_reject_students_and_anonymous(client, method, path):
    kwargs = {"json": {"message": "hi"}} if method == "post" else {}
    assert getattr(client, method)(path, **kwargs).status_code == 401
    assert getattr(client, method)(path, headers=STUDENT, **kwargs).status_code == 403


def test_inbox_is_empty_to_start(client):
    assert inbox(client) == []


def test_inbox_describes_each_thread(client, add_user):
    add_user(7, email="nimal@example.com", full_name="Nimal Perera")
    add_user(8, email="kamal@example.com")
    send(client, "from guest", session_id=GUEST_SESSION)
    send(client, "from nimal", headers=STUDENT)
    send(client, "from kamal", headers=OTHER_STUDENT)
    send(client, "nimal again", headers=STUDENT)

    by_user = {c["user_id"]: c for c in inbox(client)}

    assert by_user[None]["student_name"] == "Guest"
    assert by_user[None]["session_id"] == GUEST_SESSION
    assert by_user[7]["student_name"] == "Nimal Perera"
    assert by_user[7]["last_message"] == "nimal again"
    assert by_user[7]["unread_count"] == 2
    # No profile name yet, so the account email is shown.
    assert by_user[8]["student_name"] == "kamal@example.com"


def test_student_with_no_replica_row_is_shown_as_student(client):
    send(client, headers=STUDENT)
    assert inbox(client)[0]["student_name"] == "Student"


def test_inbox_lists_most_recent_activity_first(client):
    send(client, "a", session_id=GUEST_SESSION)
    send(client, "b", session_id=OTHER_SESSION)
    send(client, "c", headers=STUDENT)
    send(client, "a again", session_id=GUEST_SESSION)
    assert [c["last_message"] for c in inbox(client)] == ["a again", "c", "b"]


def test_unread_count_ignores_admin_replies(client):
    conv_id = send(client, headers=STUDENT).json()["conversation_id"]
    client.post(
        f"/chat/admin/conversations/{conv_id}/reply",
        json={"message": "reply"},
        headers=ADMIN,
    )
    assert inbox(client)[0]["unread_count"] == 1
    assert inbox(client)[0]["last_message"] == "reply"


def test_opening_a_thread_marks_student_messages_read(client):
    conv_id = send(client, "q1", headers=STUDENT).json()["conversation_id"]
    send(client, "q2", headers=STUDENT)
    client.post(
        f"/chat/admin/conversations/{conv_id}/reply",
        json={"message": "answer"},
        headers=ADMIN,
    )

    messages = client.get(
        f"/chat/admin/conversations/{conv_id}/messages", headers=ADMIN
    ).json()

    assert [m["message"] for m in messages] == ["q1", "q2", "answer"]
    assert all(m["is_read"] for m in messages if not m["is_from_admin"])
    assert inbox(client)[0]["unread_count"] == 0


def test_messages_for_unknown_thread_is_404(client):
    response = client.get("/chat/admin/conversations/999/messages", headers=ADMIN)
    assert response.status_code == 404
    assert response.json()["detail"] == {"error": "CONVERSATION_NOT_FOUND"}


def test_admin_reply_reaches_the_student(client):
    conv_id = send(client, headers=STUDENT).json()["conversation_id"]

    response = client.post(
        f"/chat/admin/conversations/{conv_id}/reply",
        json={"message": "  We're on it  "},
        headers=ADMIN,
    )

    assert response.status_code == 200
    reply = response.json()
    assert reply["is_from_admin"] is True
    assert reply["sender_id"] == 1
    assert reply["message"] == "We're on it"
    assert history(client, headers=STUDENT)[-1]["message"] == "We're on it"


def test_reply_to_unknown_thread_is_404(client):
    response = client.post(
        "/chat/admin/conversations/999/reply", json={"message": "hi"}, headers=ADMIN
    )
    assert response.status_code == 404


@pytest.mark.parametrize("message", ["", "x" * 4001], ids=["empty", "too-long"])
def test_reply_length_is_enforced(client, message):
    conv_id = send(client, headers=STUDENT).json()["conversation_id"]
    response = client.post(
        f"/chat/admin/conversations/{conv_id}/reply",
        json={"message": message},
        headers=ADMIN,
    )
    assert response.status_code == 422


def test_admin_can_open_an_existing_student_thread(client, add_user):
    add_user(7, full_name="Nimal Perera")
    conv_id = send(client, "hello", headers=STUDENT).json()["conversation_id"]

    response = client.get("/chat/admin/conversations/student/7", headers=ADMIN)

    assert response.status_code == 200
    assert response.json()["id"] == conv_id
    assert response.json()["last_message"] == "hello"


def test_admin_can_message_a_known_student_first(client, add_user, db):
    add_user(7, full_name="Nimal Perera")

    first = client.get("/chat/admin/conversations/student/7", headers=ADMIN).json()
    again = client.get("/chat/admin/conversations/student/7", headers=ADMIN).json()

    assert first["id"] == again["id"]
    assert first["student_name"] == "Nimal Perera"
    assert first["last_message"] is None
    assert first["unread_count"] == 0
    assert db.query(ChatConversation).count() == 1


def test_opening_a_thread_for_an_unknown_student_is_404(client, db):
    response = client.get("/chat/admin/conversations/student/404", headers=ADMIN)
    assert response.status_code == 404
    assert response.json()["detail"] == {"error": "USER_NOT_FOUND"}
    assert db.query(ChatConversation).count() == 0


def test_admin_can_delete_a_thread_and_its_messages(client, db):
    conv_id = send(client, headers=STUDENT).json()["conversation_id"]
    send(client, "second", headers=STUDENT)

    response = client.delete(f"/chat/admin/conversations/{conv_id}", headers=ADMIN)

    assert response.status_code == 204
    assert db.query(ChatConversation).count() == 0
    assert db.query(ChatMessage).count() == 0
    assert history(client, headers=STUDENT) == []


def test_deleting_an_unknown_thread_is_404(client):
    assert (
        client.delete("/chat/admin/conversations/999", headers=ADMIN).status_code == 404
    )
