"""Auth service gaps on in-memory SQLite: admin-only routes, account status and
deletion, every login/refresh failure, Google sign-in linking, password set,
change and reset edge cases, verification resend rules, OTP endpoints, the
token/redis helpers, startup and the account resync."""

import hashlib
import io
import json
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import main
import pytest
import router
import services
from database import Base
from fastapi.testclient import TestClient
from main import app
from models import RefreshToken, User, UserRole
from schemas import check_password_strength
from services import (
    EmailVerificationService,
    GoogleAuthService,
    MemoryRedisClient,
    PasswordResetService,
    TokenService,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import auth
from shared import events as ev

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


# https, so the Secure refresh-token cookie is sent back like a browser would.
client = TestClient(app, base_url="https://testserver")

PASSWORD = "Password@123"
_real_gensalt = bcrypt.gensalt


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setitem(app.dependency_overrides, router.get_db, override_get_db)
    monkeypatch.setattr(main, "SessionLocal", TestingSessionLocal)
    # Cheap hashes keep the suite fast; the algorithm is the same.
    monkeypatch.setattr(bcrypt, "gensalt", lambda: _real_gensalt(4))
    monkeypatch.setattr(services, "redis_client", MemoryRedisClient())
    client.cookies.clear()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    published = []
    monkeypatch.setattr(
        router, "publish_event", lambda key, payload: published.append((key, payload))
    )
    return published


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _user(db, email="ama@example.com", password=PASSWORD, **fields) -> User:
    fields.setdefault("role", UserRole.student)
    fields.setdefault("is_active", True)
    fields.setdefault("is_email_verified", True)
    hashed = (
        bcrypt.hashpw(password.encode(), _real_gensalt(4)).decode()
        if password
        else None
    )
    user = User(email=email, hashed_password=hashed, **fields)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _auth(user_id: int, role: str) -> dict:
    token = jwt.encode(
        {"sub": str(user_id), "role": role}, auth.SECRET_KEY, algorithm=auth.ALGORITHM
    )
    return {"Authorization": f"Bearer {token}"}


ADMIN = _auth(9999, "admin")


def _keys(sent):
    return [k for k, _ in sent]


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def test_given_running_service_when_health_requested_then_reports_ok():
    # Act and assert
    assert client.get("/health").json() == {"status": "ok", "service": "auth_service"}


def test_given_service_starting_when_startup_runs_then_tables_created_and_resync_scheduled(
    monkeypatch,
):
    # Arrange
    startup_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    calls = []
    monkeypatch.setattr(main, "engine", startup_engine)
    monkeypatch.setattr(main.messaging, "resync_later", lambda task: calls.append(task))

    main.on_startup()  # SQLite rejects the ALTER COLUMN migration; logged, not fatal

    # Act and assert
    with startup_engine.connect() as conn:
        assert "users" in startup_engine.dialect.get_table_names(conn)

    # Act and assert
    assert calls == [main._announce_all_accounts]
    startup_engine.dispose()


def test_given_table_creation_fails_when_startup_runs_then_service_still_starts(
    monkeypatch,
):
    # Arrange
    calls = []

    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main.messaging, "resync_later", lambda task: calls.append(task))

    # Act
    main.on_startup()

    # Assert
    assert calls == [main._announce_all_accounts]


def test_given_existing_accounts_when_resync_runs_then_every_account_is_announced_again(
    db, sent
):
    # Arrange
    _user(db, "a@example.com")
    _user(db, "b@example.com", role=UserRole.teacher, is_active=False)

    main._announce_all_accounts()

    # Act and assert
    assert _keys(sent) == [ev.AUTH_ACCOUNT_UPDATED] * 2
    assert [(p["email"], p["role"], p["is_active"]) for _, p in sent] == [
        ("a@example.com", "student", True),
        ("b@example.com", "teacher", False),
    ]


# ---------------------------------------------------------------------------
# Registration and verification
# ---------------------------------------------------------------------------


def test_given_admin_role_in_public_registration_when_registering_then_admin_is_never_created(
    db, sent
):
    # Act
    resp = client.post(
        "/register",
        json={"email": "sneaky@example.com", "password": PASSWORD, "role": "admin"},
    )

    # Assert
    assert resp.status_code == 200 and resp.json()["role"] == "student"
    assert resp.json()["is_email_verified"] is False

    # Act
    registered = dict(sent)[ev.AUTH_USER_REGISTERED]

    # Assert
    assert registered["role"] == "student" and registered["profile"] is None


def test_given_teacher_role_when_registering_then_teacher_account_is_created(db):
    # Act
    resp = client.post(
        "/register",
        json={"email": "t@example.com", "password": PASSWORD, "role": "teacher"},
    )

    # Assert
    assert resp.json()["role"] == "teacher"


def test_given_registered_email_when_registering_again_then_400_email_exists(db):
    # Arrange
    _user(db)

    # Act
    resp = client.post(
        "/register", json={"email": "ama@example.com", "password": PASSWORD}
    )

    # Assert
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "EMAIL_EXISTS"


def test_given_profile_with_empty_fields_when_registering_then_empty_fields_are_dropped(
    sent,
):
    # Act
    client.post(
        "/register",
        json={
            "email": "n@example.com",
            "password": PASSWORD,
            "profile": {"first_name": "Nimal", "last_name": "Perera", "school": None},
        },
    )

    # Assert
    assert dict(sent)[ev.AUTH_USER_REGISTERED]["profile"] == {
        "first_name": "Nimal",
        "last_name": "Perera",
    }


@pytest.mark.parametrize(
    "password, message",
    [
        ("Short@1", "at least 8 characters"),
        ("password@123", "uppercase"),
        ("PASSWORD@123", "lowercase"),
        ("Password@abc", "number"),
        ("Password123", "special character"),
    ],
)
def test_given_weak_passwords_when_registering_then_each_is_rejected_with_422(
    password, message
):
    # Act and assert
    with pytest.raises(ValueError, match=message):
        check_password_strength(password)
    assert (
        client.post(
            "/register", json={"email": "x@example.com", "password": password}
        ).status_code
        == 422
    )


def test_given_link_for_changed_address_when_verifying_email_then_400_invalid_token(
    db, sent
):
    # Arrange
    user = _user(db, is_email_verified=False)
    token = EmailVerificationService.create_token(user.id, "old@example.com")

    # Act
    resp = client.post("/verify-email", json={"token": token})

    # Assert
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "INVALID_VERIFICATION_TOKEN"
    assert sent == []


def test_given_verified_account_when_verifying_again_then_account_is_announced_once(
    db, sent
):
    # Arrange
    user = _user(db, is_email_verified=False)
    token = EmailVerificationService.create_token(user.id, user.email)

    # Act and assert
    assert client.post("/verify-email", json={"token": token}).status_code == 200
    assert client.post("/verify-email", json={"token": token}).status_code == 200

    # Act and assert
    assert _keys(sent) == [ev.AUTH_ACCOUNT_UPDATED]


def test_given_deleted_account_when_verifying_email_then_400(db):
    # Arrange
    token = EmailVerificationService.create_token(404, "gone@example.com")

    # Act and assert
    assert client.post("/verify-email", json={"token": token}).status_code == 400


@pytest.mark.parametrize(
    "fields",
    [
        {"is_email_verified": True},
        {"is_active": False},
        {
            "password": None,
            "google_id": "g1",
        },  # Google-only accounts are verified by Google
    ],
)
def test_given_verified_or_unknown_account_when_resending_verification_then_no_link_is_sent(
    db, sent, fields
):
    # Arrange
    password = fields.pop("password", PASSWORD)
    fields.setdefault("is_email_verified", False)
    _user(db, password=password, **fields)

    # Act
    resp = client.post("/resend-verification", json={"email": "ama@example.com"})

    # Assert
    assert resp.status_code == 200
    assert sent == []


def test_given_repeated_or_unknown_address_when_resending_verification_then_throttled_and_response_is_the_same(
    db, sent
):
    # Arrange
    _user(db, is_email_verified=False)
    first = client.post("/resend-verification", json={"email": "ama@example.com"})
    second = client.post("/resend-verification", json={"email": "ama@example.com"})

    # Act
    unknown = client.post("/resend-verification", json={"email": "who@example.com"})

    # Assert
    assert first.json() == second.json() == unknown.json()
    assert len(sent) == 1
    assert sent[0][1]["verify_url"].startswith(
        router.settings.FRONTEND_URL.rstrip("/") + "/verify-email?token="
    )


# ---------------------------------------------------------------------------
# Admin account management
# ---------------------------------------------------------------------------


def test_given_non_admin_when_admin_route_requested_then_403():
    # Act and assert
    assert client.get("/admin", headers=ADMIN).json() == {
        "message": "Welcome Admin",
        "user_id": "9999",
    }
    assert client.get("/admin", headers=_auth(1, "teacher")).status_code == 403


def test_given_admin_when_creating_admin_then_verified_admin_with_profile_can_log_in(
    db, sent
):
    # Act
    resp = client.post(
        "/admin/create-admin",
        json={
            "email": "boss@example.com",
            "password": "whatever1",
            "first_name": "Head",
            "last_name": "Office",
        },
        headers=ADMIN,
    )

    # Assert
    assert resp.status_code == 201
    assert (resp.json()["role"], resp.json()["is_email_verified"]) == ("admin", True)

    # Act
    registered = dict(sent)[ev.AUTH_USER_REGISTERED]

    # Assert
    assert registered["profile"] == {"first_name": "Head", "last_name": "Office"}

    # Act
    login = client.post(
        "/login", json={"email": "boss@example.com", "password": "whatever1"}
    )

    # Assert
    assert login.status_code == 200


def test_given_non_admin_duplicate_email_or_bad_body_when_creating_admin_then_errors_returned(
    db,
):
    # Arrange
    _user(db)
    body = {
        "email": "ama@example.com",
        "password": "whatever1",
        "first_name": "A",
        "last_name": "B",
    }

    # Act
    resp = client.post("/admin/create-admin", json=body, headers=ADMIN)

    # Assert
    assert resp.json()["detail"]["error"] == "EMAIL_EXISTS"

    # Act and assert
    assert (
        client.post(
            "/admin/create-admin", json=body, headers=_auth(1, "student")
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/admin/create-admin", json={**body, "password": "short"}, headers=ADMIN
        ).status_code
        == 422
    )


def test_given_admin_when_deactivating_account_then_change_is_announced_and_login_is_blocked(
    db, sent
):
    # Arrange
    user = _user(db)

    # Act
    resp = client.put(
        f"/users/{user.id}/status", json={"is_active": False}, headers=ADMIN
    )

    # Assert
    assert resp.json() == {"user_id": user.id, "is_active": False}
    assert sent[-1][1]["is_active"] is False

    # Act
    login = client.post("/login", json={"email": user.email, "password": PASSWORD})

    # Assert
    assert login.status_code == 403
    assert login.json()["detail"]["error"] == "ACCOUNT_DEACTIVATED"

    # Act and assert
    assert (
        client.put(
            "/users/404/status", json={"is_active": True}, headers=ADMIN
        ).status_code
        == 404
    )


def test_given_admin_when_deleting_account_then_204_and_unknown_user_is_404(db):
    # Arrange
    user = _user(db)

    # Act and assert
    assert client.delete(f"/users/{user.id}", headers=ADMIN).status_code == 204

    # Act and assert
    assert db.query(User).count() == 0

    # Act
    resp = client.delete(f"/users/{user.id}", headers=ADMIN)

    # Assert
    assert resp.json()["detail"]["error"] == "USER_NOT_FOUND"


# ---------------------------------------------------------------------------
# Login, refresh, logout
# ---------------------------------------------------------------------------


def test_given_valid_credentials_when_logging_in_then_token_and_secure_cookie_are_issued(
    db,
):
    # Arrange
    user = _user(db, role=UserRole.teacher)

    # Act
    resp = client.post("/login", json={"email": user.email, "password": PASSWORD})

    # Assert
    assert resp.status_code == 200

    # Act
    claims = jwt.decode(
        resp.json()["access_token"],
        router.settings.SECRET_KEY,
        algorithms=[router.settings.ALGORITHM],
    )

    # Assert
    assert (claims["sub"], claims["role"], claims["email"]) == (
        str(user.id),
        "TEACHER",
        user.email,
    )

    # Act
    cookie = resp.headers["set-cookie"].lower()

    # Assert
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_given_google_only_account_when_logging_in_with_password_then_400_google_only_account(
    db,
):
    # Arrange
    _user(db, password=None, google_id="g1", auth_provider="google")

    # Act
    resp = client.post(
        "/login", json={"email": "ama@example.com", "password": PASSWORD}
    )

    # Assert
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "GOOGLE_ONLY_ACCOUNT"


def test_given_refresh_cookie_when_refreshing_then_token_is_rotated(db):
    # Arrange
    user = _user(db)
    client.post("/login", json={"email": user.email, "password": PASSWORD})
    old = client.cookies.get("refresh_token")

    # Act
    resp = client.post("/refresh")

    # Assert
    assert resp.status_code == 200

    # Act and assert
    assert client.cookies.get("refresh_token") != old

    # Act
    revoked = db.query(RefreshToken).filter_by(is_revoked=True).count()

    # Assert
    assert revoked == 1


def test_given_missing_or_invalid_refresh_token_when_refreshing_then_errors_returned(
    db,
):
    # Act
    resp = client.post("/refresh")

    # Assert
    assert resp.json()["detail"]["error"] == "MISSING_REFRESH_TOKEN"

    # Act
    bad = client.post("/refresh", json={"refresh_token": "nope"})

    # Assert
    assert bad.json()["detail"]["error"] == "INVALID_REFRESH_TOKEN"


def test_given_expired_refresh_token_when_refreshing_then_401_expired(db):
    # Arrange
    user = _user(db)
    token = TokenService.create_refresh_token(db, user.id)
    row = db.query(RefreshToken).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()

    # Act
    resp = client.post("/refresh", json={"refresh_token": token})

    # Assert
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "REFRESH_TOKEN_EXPIRED"


def test_given_deleted_account_when_refreshing_then_user_not_found(db):
    # Arrange
    user = _user(db)
    token = TokenService.create_refresh_token(db, user.id)
    db.delete(user)
    db.commit()

    # Act
    resp = client.post("/refresh", json={"refresh_token": token})

    # Assert
    assert resp.json()["detail"]["error"] == "USER_NOT_FOUND"


def test_given_no_cookie_when_logging_out_then_request_still_succeeds():
    # Act and assert
    assert client.post("/logout").json() == {"message": "Successfully logged out"}


# ---------------------------------------------------------------------------
# Google sign-in
# ---------------------------------------------------------------------------


@pytest.fixture
def google(monkeypatch):
    """What Google's userinfo endpoint will say about the next token."""
    info = {
        "google_id": "g-123",
        "email": "ama@example.com",
        "full_name": "Ama Kumari Perera",
        "picture": "https://img/ama.png",
        "email_verified": True,
    }
    monkeypatch.setattr(
        GoogleAuthService, "verify_access_token", staticmethod(lambda token: info)
    )
    return info


def test_given_invalid_google_token_when_signing_in_with_google_then_401(monkeypatch):
    # Arrange
    monkeypatch.setattr(
        GoogleAuthService, "verify_access_token", staticmethod(lambda token: None)
    )

    # Act
    resp = client.post("/google", json={"access_token": "bad"})

    # Assert
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "INVALID_GOOGLE_TOKEN"


def test_given_new_google_user_without_name_when_signing_in_then_email_address_is_used_as_name(
    db, sent, google
):
    # Arrange
    google.update(full_name="", picture="")

    # Act
    resp = client.post("/google", json={"access_token": "t"})

    # Assert
    assert resp.status_code == 200

    # Act
    profile = dict(sent)[ev.AUTH_USER_REGISTERED]["profile"]

    # Assert
    assert profile == {"first_name": "ama", "last_name": "", "avatar_url": None}

    # Act
    user = db.query(User).one()

    # Assert
    assert (user.auth_provider, user.hashed_password, user.role) == (
        "google",
        None,
        UserRole.student,
    )


def test_given_verified_password_account_when_signing_in_with_google_then_accounts_are_linked(
    db, sent, google
):
    # Arrange
    user = _user(db)

    # Act and assert
    assert client.post("/google", json={"access_token": "t"}).status_code == 200
    db.refresh(user)

    # Act and assert
    assert user.google_id == "g-123"
    assert user.hashed_password is not None  # both sign-in methods now work
    assert user.auth_provider == "email"
    assert sent == []  # nothing about the account changed for other services


def test_given_returning_google_user_when_signing_in_then_account_is_found_by_google_id(
    db, google
):
    # Arrange
    _user(db, email="old@example.com", password=None, google_id="g-123")

    client.post("/google", json={"access_token": "t"})

    # Act and assert
    assert db.query(User).count() == 1


def test_given_passwordless_account_when_signing_in_with_google_then_it_becomes_a_google_account(
    db, google
):
    # Arrange
    google["email_verified"] = False

    # Act
    user = _user(db, password=None, is_email_verified=True)
    client.post("/google", json={"access_token": "t"})
    db.refresh(user)

    # Assert
    assert (user.google_id, user.auth_provider) == ("g-123", "google")


def test_given_deactivated_google_account_when_signing_in_then_403(db, google):
    # Arrange
    _user(db, google_id="g-123", is_active=False)

    # Act
    resp = client.post("/google", json={"access_token": "t"})

    # Assert
    assert resp.status_code == 403


def test_given_access_token_when_google_userinfo_requested_then_profile_is_returned(
    monkeypatch,
):
    # Arrange
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return Response(
            json.dumps(
                {"sub": "g1", "email": "a@example.com", "email_verified": True}
            ).encode()
        )

    monkeypatch.setattr(services.urllib.request, "urlopen", urlopen)

    # Act and assert
    assert GoogleAuthService.verify_access_token("tok") == {
        "google_id": "g1",
        "email": "a@example.com",
        "full_name": "",
        "picture": "",
        "email_verified": True,
    }
    assert seen == {"auth": "Bearer tok", "timeout": 5}

    monkeypatch.setattr(
        services.urllib.request,
        "urlopen",
        lambda r, timeout: Response(b'{"sub": "g1"}'),
    )

    # Act and assert
    assert GoogleAuthService.verify_access_token("tok") is None  # no address

    def down(request, timeout):
        raise services.urllib.error.URLError("offline")

    monkeypatch.setattr(services.urllib.request, "urlopen", down)

    # Act and assert
    assert GoogleAuthService.verify_access_token("tok") is None


# ---------------------------------------------------------------------------
# /me, set-password, change-password
# ---------------------------------------------------------------------------


def test_given_google_only_user_when_me_requested_then_profile_flags_returned_and_unknown_user_is_404(
    db,
):
    # Arrange
    user = _user(db, password=None, google_id="g1", auth_provider="google")

    # Act
    data = client.get("/me", headers=_auth(user.id, "student")).json()

    # Assert
    assert (data["has_password"], data["auth_provider"], data["email"]) == (
        False,
        "google",
        user.email,
    )

    # Act and assert
    assert client.get("/me", headers=_auth(404, "student")).status_code == 404


def test_given_google_user_when_setting_a_password_then_login_with_it_works_and_second_set_is_refused(
    db,
):
    # Arrange
    user = _user(db, password=None, google_id="g1", auth_provider="google")
    headers = _auth(user.id, "student")

    # Act
    resp = client.post(
        "/set-password", json={"new_password": PASSWORD}, headers=headers
    )

    # Assert
    assert resp.status_code == 200

    # Act and assert
    assert (
        client.post(
            "/login", json={"email": user.email, "password": PASSWORD}
        ).status_code
        == 200
    )

    # Act
    again = client.post(
        "/set-password", json={"new_password": PASSWORD}, headers=headers
    )

    # Assert
    assert again.json()["detail"]["error"] == "PASSWORD_ALREADY_SET"


def test_given_invalid_password_or_missing_user_when_setting_password_then_422_or_404():
    # Arrange
    headers = _auth(404, "student")

    # Act and assert
    assert (
        client.post(
            "/set-password", json={"new_password": "weakpass"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/set-password", json={"new_password": PASSWORD}, headers=headers
        ).status_code
        == 404
    )


def test_given_correct_current_password_when_changing_password_then_new_password_works_and_wrong_one_is_refused(
    db,
):
    # Arrange
    user = _user(db)
    headers = _auth(user.id, "student")

    # Act
    wrong = client.post(
        "/change-password",
        json={"current_password": "Wrong@1234", "new_password": "NewPass@456"},
        headers=headers,
    )

    # Assert
    assert wrong.json()["detail"]["error"] == "INVALID_CREDENTIALS"

    # Act
    ok = client.post(
        "/change-password",
        json={"current_password": PASSWORD, "new_password": "NewPass@456"},
        headers=headers,
    )

    # Assert
    assert ok.status_code == 200

    # Act and assert
    assert (
        client.post(
            "/login", json={"email": user.email, "password": "NewPass@456"}
        ).status_code
        == 200
    )


def test_given_account_without_password_when_changing_password_then_404_no_password_set(
    db,
):
    # Arrange
    user = _user(db, password=None, google_id="g1")
    body = {"current_password": "x", "new_password": "NewPass@456"}

    # Act
    resp = client.post("/change-password", json=body, headers=_auth(user.id, "student"))

    # Assert
    assert resp.json()["detail"]["error"] == "NO_PASSWORD_SET"

    # Act and assert
    assert (
        client.post(
            "/change-password", json=body, headers=_auth(404, "student")
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Forgot / reset password
# ---------------------------------------------------------------------------


def test_given_deactivated_account_when_requesting_password_reset_then_no_link_is_sent(
    db, sent
):
    # Arrange
    _user(db, is_active=False)

    # Act
    resp = client.post("/forgot-password", json={"email": "ama@example.com"})

    # Assert
    assert resp.status_code == 200 and sent == []


def _reset_body(token, password="NewPass@456", confirm=None):
    return {
        "token": token,
        "new_password": password,
        "confirm_password": confirm or password,
    }


def test_given_account_deactivated_after_link_when_resetting_password_then_token_is_invalid(
    db,
):
    # Arrange
    user = _user(db)
    token = PasswordResetService.create_token(user.id, user.hashed_password)
    user.is_active = False
    db.commit()

    # Act
    resp = client.post("/reset-password", json=_reset_body(token))

    # Assert
    assert resp.json()["detail"]["error"] == "INVALID_RESET_TOKEN"


def test_given_mismatched_passwords_when_resetting_password_then_422(db):
    # Act
    resp = client.post(
        "/reset-password", json=_reset_body("t", confirm="Different@123")
    )

    # Assert
    assert resp.status_code == 422


def test_given_verified_account_when_password_reset_then_account_is_not_announced_again(
    db, sent
):
    # Arrange
    user = _user(db)
    token = PasswordResetService.create_token(user.id, user.hashed_password)

    # Act and assert
    assert client.post("/reset-password", json=_reset_body(token)).status_code == 200

    # Act and assert
    assert _keys(sent) == ["auth.password-changed"]


def test_given_active_sessions_when_password_reset_then_every_session_is_signed_out(db):
    # Arrange
    user = _user(db)
    services.SessionService.create_session(user.id, "STUDENT")
    TokenService.create_refresh_token(db, user.id)
    token = PasswordResetService.create_token(user.id, user.hashed_password)

    client.post("/reset-password", json=_reset_body(token))

    # Act and assert
    assert services.redis_client.keys(f"session:{user.id}:*") == []
    assert db.query(RefreshToken).filter_by(is_revoked=False).count() == 0


# ---------------------------------------------------------------------------
# OTP endpoints
# ---------------------------------------------------------------------------


def test_given_requested_otp_when_verified_then_it_works_once_and_second_use_is_rejected():
    # Act
    code = client.post("/otp/request", json={"user_id": 7}).json()["code"]

    # Assert
    assert len(code) == 6 and code.isdigit()

    # Act
    wrong = client.post("/otp/verify", json={"user_id": 7, "code": "x"})

    # Assert
    assert wrong.json()["detail"]["error"] == "INVALID_OTP"

    # Act and assert
    assert (
        client.post("/otp/verify", json={"user_id": 7, "code": code}).status_code == 200
    )

    # Act
    reused = client.post("/otp/verify", json={"user_id": 7, "code": code})

    # Assert
    assert reused.json()["detail"]["error"] == "OTP_EXPIRED"


def test_given_otp_for_one_purpose_when_used_for_another_then_it_is_rejected():
    # Arrange
    code = client.post(
        "/otp/request", json={"user_id": 7, "purpose": "password_reset"}
    ).json()["code"]

    # Act
    resp = client.post("/otp/verify", json={"user_id": 7, "code": code})

    # Assert
    assert resp.json()["detail"]["error"] == "OTP_EXPIRED"

    # Act and assert
    assert (
        client.post("/otp/request", json={"user_id": 7, "purpose": "phone"}).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# Token and redis helpers
# ---------------------------------------------------------------------------


def test_given_access_token_without_email_when_created_then_token_is_still_valid():
    # Act
    claims = jwt.decode(
        TokenService.create_access_token(3, "admin"),
        router.settings.SECRET_KEY,
        algorithms=[router.settings.ALGORITHM],
    )

    # Assert
    assert "email" not in claims and claims["role"] == "ADMIN"


def _signed(payload):
    return jwt.encode(
        payload, router.settings.SECRET_KEY, algorithm=router.settings.ALGORITHM
    )


def test_given_reset_tokens_when_parsed_then_valid_ones_are_accepted_and_bad_ones_rejected():
    # Act and assert
    assert PasswordResetService.read_token("garbage") is None
    assert (
        PasswordResetService.read_token(_signed({"sub": "1", "purpose": "other"}))
        is None
    )
    assert (
        PasswordResetService.read_token(
            _signed({"sub": "x", "purpose": "password_reset"})
        )
        is None
    )

    parsed = PasswordResetService.read_token(
        _signed({"sub": "4", "purpose": "password_reset"})
    )

    # Act and assert
    assert parsed == (4, "")
    assert PasswordResetService.matches(None, PasswordResetService._fingerprint(None))


def test_given_verification_tokens_when_parsed_then_valid_ones_are_accepted_and_bad_ones_rejected():
    # Act and assert
    assert (
        EmailVerificationService.read_token(
            _signed({"sub": "1", "purpose": "email_verification"})
        )
        is None
    )

    reset = PasswordResetService.create_token(1, "hash")

    # Act and assert
    assert EmailVerificationService.read_token(reset) is None


def test_given_in_memory_redis_when_values_stored_and_read_then_they_behave_like_redis():
    # Arrange
    r = MemoryRedisClient()
    r.set("a", 1, ex=60)
    r.set("b", "2")

    # Act and assert
    assert (r.get("a"), r.get("b"), r.get("missing")) == ("1", "2", None)
    r._store["a"] = ("1", datetime.now(timezone.utc) - timedelta(seconds=1))

    # Act and assert
    assert r.get("a") is None
    r.set("session:1:x", "v", ex=60)
    r._store["session:1:old"] = ("v", datetime.now(timezone.utc) - timedelta(seconds=1))

    # Act and assert
    assert r.keys("session:1:*") == ["session:1:x"]
    assert "session:1:old" not in r._store


def test_given_issued_refresh_token_when_stored_then_only_its_hash_is_saved(db):
    # Arrange
    token = TokenService.create_refresh_token(db, 1)

    row = db.query(RefreshToken).one()

    # Act and assert
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in row.token_hash
