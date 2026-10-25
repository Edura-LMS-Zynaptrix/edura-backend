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


def test_health():
    assert client.get("/health").json() == {"status": "ok", "service": "auth_service"}


def test_startup_creates_tables_and_schedules_the_resync(monkeypatch):
    startup_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    calls = []
    monkeypatch.setattr(main, "engine", startup_engine)
    monkeypatch.setattr(main.messaging, "resync_later", lambda task: calls.append(task))

    main.on_startup()  # SQLite rejects the ALTER COLUMN migration; logged, not fatal

    with startup_engine.connect() as conn:
        assert "users" in startup_engine.dialect.get_table_names(conn)
    assert calls == [main._announce_all_accounts]
    startup_engine.dispose()


def test_startup_survives_create_all_failure(monkeypatch):
    calls = []

    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main.messaging, "resync_later", lambda task: calls.append(task))
    main.on_startup()
    assert calls == [main._announce_all_accounts]


def test_resync_reannounces_every_account(db, sent):
    _user(db, "a@example.com")
    _user(db, "b@example.com", role=UserRole.teacher, is_active=False)
    main._announce_all_accounts()
    assert _keys(sent) == [ev.AUTH_ACCOUNT_UPDATED] * 2
    assert [(p["email"], p["role"], p["is_active"]) for _, p in sent] == [
        ("a@example.com", "student", True),
        ("b@example.com", "teacher", False),
    ]


# ---------------------------------------------------------------------------
# Registration and verification
# ---------------------------------------------------------------------------


def test_public_registration_can_never_create_an_admin(db, sent):
    resp = client.post(
        "/register",
        json={"email": "sneaky@example.com", "password": PASSWORD, "role": "admin"},
    )
    assert resp.status_code == 200 and resp.json()["role"] == "student"
    assert resp.json()["is_email_verified"] is False
    registered = dict(sent)[ev.AUTH_USER_REGISTERED]
    assert registered["role"] == "student" and registered["profile"] is None


def test_teachers_may_self_register(db):
    resp = client.post(
        "/register",
        json={"email": "t@example.com", "password": PASSWORD, "role": "teacher"},
    )
    assert resp.json()["role"] == "teacher"


def test_duplicate_registration(db):
    _user(db)
    resp = client.post(
        "/register", json={"email": "ama@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "EMAIL_EXISTS"


def test_registration_profile_drops_empty_fields(sent):
    client.post(
        "/register",
        json={
            "email": "n@example.com",
            "password": PASSWORD,
            "profile": {"first_name": "Nimal", "last_name": "Perera", "school": None},
        },
    )
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
def test_password_strength_rules(password, message):
    with pytest.raises(ValueError, match=message):
        check_password_strength(password)
    assert (
        client.post(
            "/register", json={"email": "x@example.com", "password": password}
        ).status_code
        == 422
    )


def test_verification_link_for_a_changed_address_is_refused(db, sent):
    user = _user(db, is_email_verified=False)
    token = EmailVerificationService.create_token(user.id, "old@example.com")
    resp = client.post("/verify-email", json={"token": token})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "INVALID_VERIFICATION_TOKEN"
    assert sent == []


def test_verifying_twice_announces_once(db, sent):
    user = _user(db, is_email_verified=False)
    token = EmailVerificationService.create_token(user.id, user.email)
    assert client.post("/verify-email", json={"token": token}).status_code == 200
    assert client.post("/verify-email", json={"token": token}).status_code == 200
    assert _keys(sent) == [ev.AUTH_ACCOUNT_UPDATED]


def test_verification_link_for_a_deleted_account(db):
    token = EmailVerificationService.create_token(404, "gone@example.com")
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
def test_resend_skips_accounts_that_need_no_link(db, sent, fields):
    password = fields.pop("password", PASSWORD)
    fields.setdefault("is_email_verified", False)
    _user(db, password=password, **fields)
    resp = client.post("/resend-verification", json={"email": "ama@example.com"})
    assert resp.status_code == 200
    assert sent == []


def test_resend_is_throttled_and_same_for_unknown_addresses(db, sent):
    _user(db, is_email_verified=False)
    first = client.post("/resend-verification", json={"email": "ama@example.com"})
    second = client.post("/resend-verification", json={"email": "ama@example.com"})
    unknown = client.post("/resend-verification", json={"email": "who@example.com"})
    assert first.json() == second.json() == unknown.json()
    assert len(sent) == 1
    assert sent[0][1]["verify_url"].startswith(
        router.settings.FRONTEND_URL.rstrip("/") + "/verify-email?token="
    )


# ---------------------------------------------------------------------------
# Admin account management
# ---------------------------------------------------------------------------


def test_admin_route():
    assert client.get("/admin", headers=ADMIN).json() == {
        "message": "Welcome Admin",
        "user_id": "9999",
    }
    assert client.get("/admin", headers=_auth(1, "teacher")).status_code == 403


def test_admin_creates_a_verified_admin_with_a_profile(db, sent):
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
    assert resp.status_code == 201
    assert (resp.json()["role"], resp.json()["is_email_verified"]) == ("admin", True)
    registered = dict(sent)[ev.AUTH_USER_REGISTERED]
    assert registered["profile"] == {"first_name": "Head", "last_name": "Office"}

    login = client.post(
        "/login", json={"email": "boss@example.com", "password": "whatever1"}
    )
    assert login.status_code == 200


def test_create_admin_errors(db):
    _user(db)
    body = {
        "email": "ama@example.com",
        "password": "whatever1",
        "first_name": "A",
        "last_name": "B",
    }
    resp = client.post("/admin/create-admin", json=body, headers=ADMIN)
    assert resp.json()["detail"]["error"] == "EMAIL_EXISTS"
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


def test_deactivating_an_account_is_announced_and_blocks_login(db, sent):
    user = _user(db)
    resp = client.put(
        f"/users/{user.id}/status", json={"is_active": False}, headers=ADMIN
    )
    assert resp.json() == {"user_id": user.id, "is_active": False}
    assert sent[-1][1]["is_active"] is False

    login = client.post("/login", json={"email": user.email, "password": PASSWORD})
    assert login.status_code == 403
    assert login.json()["detail"]["error"] == "ACCOUNT_DEACTIVATED"
    assert (
        client.put(
            "/users/404/status", json={"is_active": True}, headers=ADMIN
        ).status_code
        == 404
    )


def test_admin_deletes_an_account(db):
    user = _user(db)
    assert client.delete(f"/users/{user.id}", headers=ADMIN).status_code == 204
    assert db.query(User).count() == 0
    resp = client.delete(f"/users/{user.id}", headers=ADMIN)
    assert resp.json()["detail"]["error"] == "USER_NOT_FOUND"


# ---------------------------------------------------------------------------
# Login, refresh, logout
# ---------------------------------------------------------------------------


def test_login_issues_a_token_and_a_secure_cookie(db):
    user = _user(db, role=UserRole.teacher)
    resp = client.post("/login", json={"email": user.email, "password": PASSWORD})
    assert resp.status_code == 200
    claims = jwt.decode(
        resp.json()["access_token"],
        router.settings.SECRET_KEY,
        algorithms=[router.settings.ALGORITHM],
    )
    assert (claims["sub"], claims["role"], claims["email"]) == (
        str(user.id),
        "TEACHER",
        user.email,
    )
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_google_only_account_cannot_use_a_password(db):
    _user(db, password=None, google_id="g1", auth_provider="google")
    resp = client.post(
        "/login", json={"email": "ama@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "GOOGLE_ONLY_ACCOUNT"


def test_refresh_from_the_cookie_rotates_it(db):
    user = _user(db)
    client.post("/login", json={"email": user.email, "password": PASSWORD})
    old = client.cookies.get("refresh_token")

    resp = client.post("/refresh")
    assert resp.status_code == 200
    assert client.cookies.get("refresh_token") != old
    revoked = db.query(RefreshToken).filter_by(is_revoked=True).count()
    assert revoked == 1


def test_refresh_errors(db):
    resp = client.post("/refresh")
    assert resp.json()["detail"]["error"] == "MISSING_REFRESH_TOKEN"
    bad = client.post("/refresh", json={"refresh_token": "nope"})
    assert bad.json()["detail"]["error"] == "INVALID_REFRESH_TOKEN"


def test_expired_refresh_token(db):
    user = _user(db)
    token = TokenService.create_refresh_token(db, user.id)
    row = db.query(RefreshToken).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "REFRESH_TOKEN_EXPIRED"


def test_refresh_for_a_deleted_account(db):
    user = _user(db)
    token = TokenService.create_refresh_token(db, user.id)
    db.delete(user)
    db.commit()
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.json()["detail"]["error"] == "USER_NOT_FOUND"


def test_logout_without_a_cookie_still_succeeds():
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


def test_invalid_google_token(monkeypatch):
    monkeypatch.setattr(
        GoogleAuthService, "verify_access_token", staticmethod(lambda token: None)
    )
    resp = client.post("/google", json={"access_token": "bad"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "INVALID_GOOGLE_TOKEN"


def test_new_google_user_without_a_name_uses_the_address(db, sent, google):
    google.update(full_name="", picture="")
    resp = client.post("/google", json={"access_token": "t"})
    assert resp.status_code == 200
    profile = dict(sent)[ev.AUTH_USER_REGISTERED]["profile"]
    assert profile == {"first_name": "ama", "last_name": "", "avatar_url": None}
    user = db.query(User).one()
    assert (user.auth_provider, user.hashed_password, user.role) == (
        "google",
        None,
        UserRole.student,
    )


def test_google_links_onto_a_verified_password_account(db, sent, google):
    user = _user(db)
    assert client.post("/google", json={"access_token": "t"}).status_code == 200
    db.refresh(user)
    assert user.google_id == "g-123"
    assert user.hashed_password is not None  # both sign-in methods now work
    assert user.auth_provider == "email"
    assert sent == []  # nothing about the account changed for other services


def test_returning_google_user_is_found_by_google_id(db, google):
    _user(db, email="old@example.com", password=None, google_id="g-123")
    client.post("/google", json={"access_token": "t"})
    assert db.query(User).count() == 1


def test_passwordless_account_becomes_a_google_account(db, google):
    google["email_verified"] = False
    user = _user(db, password=None, is_email_verified=True)
    client.post("/google", json={"access_token": "t"})
    db.refresh(user)
    assert (user.google_id, user.auth_provider) == ("g-123", "google")


def test_deactivated_google_account(db, google):
    _user(db, google_id="g-123", is_active=False)
    resp = client.post("/google", json={"access_token": "t"})
    assert resp.status_code == 403


def test_google_userinfo_call(monkeypatch):
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
    assert GoogleAuthService.verify_access_token("tok") is None  # no address

    def down(request, timeout):
        raise services.urllib.error.URLError("offline")

    monkeypatch.setattr(services.urllib.request, "urlopen", down)
    assert GoogleAuthService.verify_access_token("tok") is None


# ---------------------------------------------------------------------------
# /me, set-password, change-password
# ---------------------------------------------------------------------------


def test_me(db):
    user = _user(db, password=None, google_id="g1", auth_provider="google")
    data = client.get("/me", headers=_auth(user.id, "student")).json()
    assert (data["has_password"], data["auth_provider"], data["email"]) == (
        False,
        "google",
        user.email,
    )
    assert client.get("/me", headers=_auth(404, "student")).status_code == 404


def test_google_user_sets_a_password_then_logs_in_with_it(db):
    user = _user(db, password=None, google_id="g1", auth_provider="google")
    headers = _auth(user.id, "student")
    resp = client.post(
        "/set-password", json={"new_password": PASSWORD}, headers=headers
    )
    assert resp.status_code == 200
    assert (
        client.post(
            "/login", json={"email": user.email, "password": PASSWORD}
        ).status_code
        == 200
    )

    again = client.post(
        "/set-password", json={"new_password": PASSWORD}, headers=headers
    )
    assert again.json()["detail"]["error"] == "PASSWORD_ALREADY_SET"


def test_set_password_validation_and_missing_user():
    headers = _auth(404, "student")
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


def test_change_password(db):
    user = _user(db)
    headers = _auth(user.id, "student")
    wrong = client.post(
        "/change-password",
        json={"current_password": "Wrong@1234", "new_password": "NewPass@456"},
        headers=headers,
    )
    assert wrong.json()["detail"]["error"] == "INVALID_CREDENTIALS"

    ok = client.post(
        "/change-password",
        json={"current_password": PASSWORD, "new_password": "NewPass@456"},
        headers=headers,
    )
    assert ok.status_code == 200
    assert (
        client.post(
            "/login", json={"email": user.email, "password": "NewPass@456"}
        ).status_code
        == 200
    )


def test_change_password_needs_an_existing_password(db):
    user = _user(db, password=None, google_id="g1")
    body = {"current_password": "x", "new_password": "NewPass@456"}
    resp = client.post("/change-password", json=body, headers=_auth(user.id, "student"))
    assert resp.json()["detail"]["error"] == "NO_PASSWORD_SET"
    assert (
        client.post(
            "/change-password", json=body, headers=_auth(404, "student")
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Forgot / reset password
# ---------------------------------------------------------------------------


def test_no_reset_link_for_a_deactivated_account(db, sent):
    _user(db, is_active=False)
    resp = client.post("/forgot-password", json={"email": "ama@example.com"})
    assert resp.status_code == 200 and sent == []


def _reset_body(token, password="NewPass@456", confirm=None):
    return {
        "token": token,
        "new_password": password,
        "confirm_password": confirm or password,
    }


def test_reset_is_refused_once_the_account_is_deactivated(db):
    user = _user(db)
    token = PasswordResetService.create_token(user.id, user.hashed_password)
    user.is_active = False
    db.commit()
    resp = client.post("/reset-password", json=_reset_body(token))
    assert resp.json()["detail"]["error"] == "INVALID_RESET_TOKEN"


def test_reset_requires_matching_passwords(db):
    resp = client.post(
        "/reset-password", json=_reset_body("t", confirm="Different@123")
    )
    assert resp.status_code == 422


def test_reset_for_a_verified_account_is_not_reannounced(db, sent):
    user = _user(db)
    token = PasswordResetService.create_token(user.id, user.hashed_password)
    assert client.post("/reset-password", json=_reset_body(token)).status_code == 200
    assert _keys(sent) == ["auth.password-changed"]


def test_reset_signs_out_every_session(db):
    user = _user(db)
    services.SessionService.create_session(user.id, "STUDENT")
    TokenService.create_refresh_token(db, user.id)
    token = PasswordResetService.create_token(user.id, user.hashed_password)

    client.post("/reset-password", json=_reset_body(token))

    assert services.redis_client.keys(f"session:{user.id}:*") == []
    assert db.query(RefreshToken).filter_by(is_revoked=False).count() == 0


# ---------------------------------------------------------------------------
# OTP endpoints
# ---------------------------------------------------------------------------


def test_otp_round_trip_is_single_use():
    code = client.post("/otp/request", json={"user_id": 7}).json()["code"]
    assert len(code) == 6 and code.isdigit()
    wrong = client.post("/otp/verify", json={"user_id": 7, "code": "x"})
    assert wrong.json()["detail"]["error"] == "INVALID_OTP"
    assert (
        client.post("/otp/verify", json={"user_id": 7, "code": code}).status_code == 200
    )
    reused = client.post("/otp/verify", json={"user_id": 7, "code": code})
    assert reused.json()["detail"]["error"] == "OTP_EXPIRED"


def test_otp_purposes_are_separate():
    code = client.post(
        "/otp/request", json={"user_id": 7, "purpose": "password_reset"}
    ).json()["code"]
    resp = client.post("/otp/verify", json={"user_id": 7, "code": code})
    assert resp.json()["detail"]["error"] == "OTP_EXPIRED"
    assert (
        client.post("/otp/request", json={"user_id": 7, "purpose": "phone"}).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# Token and redis helpers
# ---------------------------------------------------------------------------


def test_access_token_without_email():
    claims = jwt.decode(
        TokenService.create_access_token(3, "admin"),
        router.settings.SECRET_KEY,
        algorithms=[router.settings.ALGORITHM],
    )
    assert "email" not in claims and claims["role"] == "ADMIN"


def _signed(payload):
    return jwt.encode(
        payload, router.settings.SECRET_KEY, algorithm=router.settings.ALGORITHM
    )


def test_reset_token_parsing():
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
    assert parsed == (4, "")
    assert PasswordResetService.matches(None, PasswordResetService._fingerprint(None))


def test_verification_token_parsing():
    assert (
        EmailVerificationService.read_token(
            _signed({"sub": "1", "purpose": "email_verification"})
        )
        is None
    )
    reset = PasswordResetService.create_token(1, "hash")
    assert EmailVerificationService.read_token(reset) is None


def test_memory_redis_client():
    r = MemoryRedisClient()
    r.set("a", 1, ex=60)
    r.set("b", "2")
    assert (r.get("a"), r.get("b"), r.get("missing")) == ("1", "2", None)
    r._store["a"] = ("1", datetime.now(timezone.utc) - timedelta(seconds=1))
    assert r.get("a") is None
    r.set("session:1:x", "v", ex=60)
    r._store["session:1:old"] = ("v", datetime.now(timezone.utc) - timedelta(seconds=1))
    assert r.keys("session:1:*") == ["session:1:x"]
    assert "session:1:old" not in r._store


def test_refresh_token_is_stored_hashed(db):
    token = TokenService.create_refresh_token(db, 1)
    row = db.query(RefreshToken).one()
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in row.token_hash
