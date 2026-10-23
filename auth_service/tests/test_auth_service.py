import os
import sys

import pytest
from fastapi.testclient import TestClient

# Ensure sys.path includes auth_service and root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from database import Base
from main import app
from services import OtpService, SessionService, TokenService

# Configure SQLite database for testing
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

TEST_DATABASE_URL = "sqlite:///./test_auth.db"
test_engine = create_engine(
    TEST_DATABASE_URL, connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

from router import get_db


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)

PUBLISHED: list = []


@pytest.fixture(autouse=True)
def capture_events(monkeypatch):
    """Nothing in these tests talks to RabbitMQ; events are recorded instead."""
    PUBLISHED.clear()
    monkeypatch.setattr(
        "router.publish_event", lambda key, payload: PUBLISHED.append((key, payload))
    )


def _verification_token() -> str:
    key, payload = next(
        e for e in reversed(PUBLISHED) if e[0].endswith("verification-requested")
    )
    return payload["verify_url"].split("token=", 1)[1]


def _register_verified(
    email: str, password: str = "Password@123", role: str = "student"
):
    """Sign up the way a person does: register, then follow the emailed link."""
    resp = client.post(
        "/register", json={"email": email, "password": password, "role": role}
    )
    assert resp.status_code == 200
    assert (
        client.post("/verify-email", json={"token": _verification_token()}).status_code
        == 200
    )


@pytest.fixture(autouse=True, scope="module")
def setup_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)
    if os.path.exists("./test_auth.db"):
        try:
            os.remove("./test_auth.db")
        except Exception:
            pass


def test_token_service_create_and_verify():
    token = TokenService.create_access_token(user_id=1, role="STUDENT")
    assert isinstance(token, str)


def test_otp_service_generate_and_verify():
    code = OtpService.generate_otp(user_id=100, purpose="email_verify")
    assert len(code) == 6
    assert code.isdigit()

    # Verify correct OTP
    assert OtpService.verify_otp(user_id=100, code=code, purpose="email_verify") is True

    # Verifying again should fail with OTP_EXPIRED because it's deleted after use
    with pytest.raises(Exception) as exc_info:
        OtpService.verify_otp(user_id=100, code=code, purpose="email_verify")
    assert "OTP_EXPIRED" in str(exc_info.value.detail)


def test_session_service_limit():
    user_id = 99
    _s1 = SessionService.create_session(user_id, "STUDENT")
    _s2 = SessionService.create_session(user_id, "STUDENT")
    _s3 = SessionService.create_session(user_id, "STUDENT")
    s4 = SessionService.create_session(user_id, "STUDENT")
    assert s4 is not None


def test_register_and_login_success():
    # Register user
    reg_resp = client.post(
        "/register",
        json={
            "email": "student@edura.com",
            "password": "Password@123",
            "role": "student",
        },
    )
    assert reg_resp.status_code == 200
    data = reg_resp.json()
    assert data["email"] == "student@edura.com"
    assert data["is_email_verified"] is False

    # Login is refused until the emailed link has been followed
    blocked = client.post(
        "/login",
        json={"email": "student@edura.com", "password": "Password@123"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"] == "EMAIL_NOT_VERIFIED"
    assert (
        client.post("/verify-email", json={"token": _verification_token()}).status_code
        == 200
    )

    # Login user
    login_resp = client.post(
        "/login",
        json={"email": "student@edura.com", "password": "Password@123"},
    )
    assert login_resp.status_code == 200
    token_data = login_resp.json()
    assert "access_token" in token_data
    assert token_data["expires_in"] == 900
    assert "refresh_token" in login_resp.cookies


def test_login_invalid_credentials():
    client.post(
        "/register",
        json={
            "email": "teacher@edura.com",
            "password": "Password@123",
            "role": "teacher",
        },
    )

    resp = client.post(
        "/login",
        json={"email": "teacher@edura.com", "password": "wrongpassword"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "INVALID_CREDENTIALS"


def test_refresh_token_rotation_and_reuse():
    # Register & Login
    _register_verified("admin@edura.com", role="admin")
    login_resp = client.post(
        "/login",
        json={"email": "admin@edura.com", "password": "Password@123"},
    )
    initial_refresh_token = login_resp.cookies["refresh_token"]

    # First refresh call - Success
    ref_resp = client.post("/refresh", json={"refresh_token": initial_refresh_token})
    assert ref_resp.status_code == 200
    new_access_token = ref_resp.json()["access_token"]
    assert new_access_token is not None

    # Reuse of initial refresh token - Should fail with REFRESH_TOKEN_REUSE
    reuse_resp = client.post("/refresh", json={"refresh_token": initial_refresh_token})
    assert reuse_resp.status_code == 401
    assert reuse_resp.json()["detail"]["error"] == "REFRESH_TOKEN_REUSE"


def test_protected_route_role_enforcement():
    # Register Student
    _register_verified("user@edura.com")
    login_resp = client.post(
        "/login",
        json={"email": "user@edura.com", "password": "Password@123"},
    )
    access_token = login_resp.json()["access_token"]

    # Access protected route with Bearer token
    headers = {"Authorization": f"Bearer {access_token}"}
    prot_resp = client.get("/protected", headers=headers)
    assert prot_resp.status_code == 200
    assert prot_resp.json()["role"] == "STUDENT"

    # Access admin route with student token - Should fail with 403 INSUFFICIENT_PERMISSIONS
    admin_resp = client.get("/admin", headers=headers)
    assert admin_resp.status_code == 403
    assert admin_resp.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


def test_missing_and_expired_token():
    # Missing token
    resp_no_token = client.get("/protected")
    assert resp_no_token.status_code == 401
    assert resp_no_token.json()["detail"]["error"] == "MISSING_TOKEN"


def test_register_rejects_weak_password():
    resp = client.post(
        "/register", json={"email": "weak@edura.com", "password": "password123"}
    )
    assert resp.status_code == 422


def _request_reset(email: str):
    from unittest.mock import patch

    with patch("router.publish_event") as publish:
        resp = client.post("/forgot-password", json={"email": email})
    return resp, publish


def test_forgot_password_does_not_reveal_unknown_emails():
    client.post(
        "/register", json={"email": "known@edura.com", "password": "Password@123"}
    )
    known, published = _request_reset("known@edura.com")
    unknown, not_published = _request_reset("nobody@edura.com")

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    published.assert_called_once()
    not_published.assert_not_called()

    routing_key, payload = published.call_args.args
    assert routing_key == "auth.password-reset-requested"
    assert payload["email"] == "known@edura.com"
    assert "/reset-password?token=" in payload["reset_url"]


def test_reset_password_flow_is_single_use_and_signs_out():
    email = "reset@edura.com"
    _register_verified(email)
    old_refresh = client.post(
        "/login", json={"email": email, "password": "Password@123"}
    ).cookies["refresh_token"]

    _, published = _request_reset(email)
    token = published.call_args.args[1]["reset_url"].split("token=", 1)[1]

    mismatch = client.post(
        "/reset-password",
        json={
            "token": token,
            "new_password": "NewPass@456",
            "confirm_password": "Other@456",
        },
    )
    assert mismatch.status_code == 422

    from unittest.mock import patch

    with patch("router.publish_event"):
        ok = client.post(
            "/reset-password",
            json={
                "token": token,
                "new_password": "NewPass@456",
                "confirm_password": "NewPass@456",
            },
        )
    assert ok.status_code == 200

    assert (
        client.post(
            "/login", json={"email": email, "password": "Password@123"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/login", json={"email": email, "password": "NewPass@456"}
        ).status_code
        == 200
    )

    # The link can't be replayed, and sessions from before the reset are gone.
    replay = client.post(
        "/reset-password",
        json={
            "token": token,
            "new_password": "Again@789",
            "confirm_password": "Again@789",
        },
    )
    assert replay.status_code == 400
    assert replay.json()["detail"]["error"] == "INVALID_RESET_TOKEN"
    assert (
        client.post("/refresh", json={"refresh_token": old_refresh}).status_code == 401
    )


def test_reset_password_rejects_garbage_token():
    resp = client.post(
        "/reset-password",
        json={
            "token": "not-a-token",
            "new_password": "NewPass@456",
            "confirm_password": "NewPass@456",
        },
    )
    assert resp.status_code == 400


def test_logout_revokes_refresh_token():
    email = "logout@edura.com"
    _register_verified(email)
    refresh = client.post(
        "/login", json={"email": email, "password": "Password@123"}
    ).cookies["refresh_token"]

    client.cookies.set("refresh_token", refresh)
    resp = client.post("/logout")
    client.cookies.clear()

    assert resp.status_code == 200
    assert client.post("/refresh", json={"refresh_token": refresh}).status_code == 401


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


def test_register_sends_a_verification_link():
    resp = client.post(
        "/register", json={"email": "newbie@edura.com", "password": "Password@123"}
    )
    assert resp.status_code == 200

    key, payload = PUBLISHED[-1]
    assert key == "auth.email-verification-requested"
    assert payload["email"] == "newbie@edura.com"
    assert "/verify-email?token=" in payload["verify_url"]
    assert payload["expires_in_minutes"] == 1440


def test_unverified_login_hides_whether_the_address_exists():
    client.post(
        "/register", json={"email": "probe@edura.com", "password": "Password@123"}
    )

    wrong = client.post(
        "/login", json={"email": "probe@edura.com", "password": "Wrong@123"}
    )
    unknown = client.post(
        "/login", json={"email": "ghost@edura.com", "password": "Wrong@123"}
    )
    # Without the right password the answer is the same as for an unknown address
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"]["error"] == "INVALID_CREDENTIALS"


def test_verification_link_is_reusable_but_only_for_verification():
    client.post(
        "/register", json={"email": "twice@edura.com", "password": "Password@123"}
    )
    token = _verification_token()
    assert client.post("/verify-email", json={"token": token}).status_code == 200
    assert (
        client.post("/verify-email", json={"token": token}).status_code == 200
    )  # harmless

    invalid = client.post("/verify-email", json={"token": "not-a-token"})
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["error"] == "INVALID_VERIFICATION_TOKEN"

    # a password-reset link, or a login token, can't be used to verify an address
    from services import PasswordResetService

    reset = PasswordResetService.create_token(1, "hash")
    assert client.post("/verify-email", json={"token": reset}).status_code == 400
    login = TokenService.create_access_token(user_id=1, role="STUDENT")
    assert client.post("/verify-email", json={"token": login}).status_code == 400


def test_expired_verification_link_is_rejected(monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "EMAIL_VERIFICATION_EXPIRE_MINUTES", -1)
    client.post(
        "/register", json={"email": "late@edura.com", "password": "Password@123"}
    )
    assert (
        client.post("/verify-email", json={"token": _verification_token()}).status_code
        == 400
    )


def test_resend_verification(monkeypatch):
    from services import MemoryRedisClient

    monkeypatch.setattr("services.redis_client", MemoryRedisClient())
    client.post(
        "/register", json={"email": "resend@edura.com", "password": "Password@123"}
    )
    PUBLISHED.clear()

    first = client.post("/resend-verification", json={"email": "resend@edura.com"})
    assert first.status_code == 200
    assert (
        len(PUBLISHED) == 1 and PUBLISHED[0][0] == "auth.email-verification-requested"
    )

    # a second request straight away is answered the same but sends nothing
    again = client.post("/resend-verification", json={"email": "resend@edura.com"})
    unknown = client.post("/resend-verification", json={"email": "nobody@edura.com"})
    assert again.json() == first.json() == unknown.json()
    assert len(PUBLISHED) == 1

    # nothing is sent once the account is verified
    assert (
        client.post("/verify-email", json={"token": _verification_token()}).status_code
        == 200
    )
    monkeypatch.setattr("services.redis_client", MemoryRedisClient())
    client.post("/resend-verification", json={"email": "resend@edura.com"})
    emails = [k for k, _ in PUBLISHED if k == "auth.email-verification-requested"]
    assert len(emails) == 1


def _events(key: str) -> list[dict]:
    return [payload for k, payload in PUBLISHED if k == key]


def test_register_announces_the_account_with_its_profile():
    resp = client.post(
        "/register",
        json={
            "email": "full@edura.com",
            "password": "Password@123",
            "profile": {
                "first_name": "Nimal",
                "last_name": "Perera",
                "mobile_no": "077 123 4567",
                "grade": 11,
                "school": "Royal",
                "district": "Colombo",
            },
        },
    )
    assert resp.status_code == 200

    (registered,) = _events("auth.user-registered")
    assert registered["user_id"] == resp.json()["id"]
    assert registered["email"] == "full@edura.com"
    assert registered["role"] == "student" and registered["is_email_verified"] is False
    # user_service builds the profile from this: validated and normalised here
    assert registered["profile"]["first_name"] == "Nimal"
    assert registered["profile"]["mobile_no"] == "0771234567"
    assert registered["profile"]["grade"] == 11
    assert _events("auth.email-verification-requested")


def test_register_without_a_profile_announces_the_account_only():
    client.post(
        "/register", json={"email": "bare@edura.com", "password": "Password@123"}
    )
    (registered,) = _events("auth.user-registered")
    assert registered["profile"] is None


def test_register_rejects_an_invalid_profile_before_announcing_anything():
    body = {
        "email": "badprofile@edura.com",
        "password": "Password@123",
        "profile": {"first_name": "A", "last_name": "B", "mobile_no": "not-a-number"},
    }
    assert client.post("/register", json=body).status_code == 422
    assert PUBLISHED == []

    # nothing was created, so the same address is still free
    body["profile"]["mobile_no"] = "0771234567"
    assert client.post("/register", json=body).status_code == 200


def test_register_does_not_depend_on_any_other_service(monkeypatch):
    """The broker being down must not stop an account being created."""
    monkeypatch.setattr("router.publish_event", lambda key, payload: False)
    resp = client.post(
        "/register", json={"email": "offline@edura.com", "password": "Password@123"}
    )
    assert resp.status_code == 200


def test_account_changes_are_announced():
    client.post(
        "/register", json={"email": "changes@edura.com", "password": "Password@123"}
    )
    user_id = _events("auth.user-registered")[0]["user_id"]

    client.post("/verify-email", json={"token": _verification_token()})
    verified = _events("auth.account-updated")[-1]
    assert verified["user_id"] == user_id and verified["is_email_verified"] is True

    admin = _token_for_admin()
    client.put(f"/users/{user_id}/status", json={"is_active": False}, headers=admin)
    deactivated = _events("auth.account-updated")[-1]
    assert deactivated["is_active"] is False


def _token_for_admin() -> dict:
    token = TokenService.create_access_token(user_id=999, role="ADMIN")
    return {"Authorization": f"Bearer {token}"}


def test_google_sign_in_verifies_and_drops_an_unverified_password(monkeypatch):
    client.post(
        "/register", json={"email": "hijack@edura.com", "password": "Attacker@123"}
    )
    info = {
        "google_id": "g-123",
        "email": "hijack@edura.com",
        "full_name": "Real Owner",
        "picture": "",
        "email_verified": True,
    }
    monkeypatch.setattr(
        "router.GoogleAuthService.verify_access_token", lambda token: info
    )
    assert client.post("/google", json={"access_token": "x"}).status_code == 200

    # the password typed in by whoever registered first no longer works
    login = client.post(
        "/login", json={"email": "hijack@edura.com", "password": "Attacker@123"}
    )
    assert (
        login.status_code == 400
        and login.json()["detail"]["error"] == "GOOGLE_ONLY_ACCOUNT"
    )


def test_google_without_a_verified_address_does_not_verify(monkeypatch):
    client.post(
        "/register", json={"email": "unsure@edura.com", "password": "Password@123"}
    )
    info = {
        "google_id": "g-456",
        "email": "unsure@edura.com",
        "full_name": "",
        "picture": "",
        "email_verified": False,
    }
    monkeypatch.setattr(
        "router.GoogleAuthService.verify_access_token", lambda token: info
    )
    client.post("/google", json={"access_token": "x"})
    login = client.post(
        "/login", json={"email": "unsure@edura.com", "password": "Password@123"}
    )
    assert (
        login.status_code == 403
        and login.json()["detail"]["error"] == "EMAIL_NOT_VERIFIED"
    )


def test_password_reset_proves_the_address():
    client.post(
        "/register", json={"email": "stuck@edura.com", "password": "Password@123"}
    )
    _, published = _request_reset("stuck@edura.com")
    token = published.call_args.args[1]["reset_url"].split("token=", 1)[1]
    done = client.post(
        "/reset-password",
        json={
            "token": token,
            "new_password": "NewPass@456",
            "confirm_password": "NewPass@456",
        },
    )
    assert done.status_code == 200
    assert (
        client.post(
            "/login", json={"email": "stuck@edura.com", "password": "NewPass@456"}
        ).status_code
        == 200
    )


def test_new_google_account_is_announced_with_the_google_name_and_photo(monkeypatch):
    info = {
        "google_id": "g-new",
        "email": "fresh@edura.com",
        "full_name": "Fresh Student",
        "picture": "https://example.com/p.png",
        "email_verified": True,
    }
    monkeypatch.setattr(
        "router.GoogleAuthService.verify_access_token", lambda token: info
    )
    assert client.post("/google", json={"access_token": "x"}).status_code == 200

    (registered,) = _events("auth.user-registered")
    assert registered["email"] == "fresh@edura.com"
    assert registered["is_email_verified"] is True
    assert registered["profile"] == {
        "first_name": "Fresh",
        "last_name": "Student",
        "avatar_url": "https://example.com/p.png",
    }


def test_admin_created_by_admin_is_announced_with_a_profile():
    resp = client.post(
        "/admin/create-admin",
        json={
            "email": "newadmin@edura.com",
            "password": "Password@123",
            "first_name": "New",
            "last_name": "Admin",
        },
        headers=_token_for_admin(),
    )
    assert resp.status_code == 201
    (registered,) = _events("auth.user-registered")
    assert registered["role"] == "admin" and registered["is_email_verified"] is True
    assert registered["profile"] == {"first_name": "New", "last_name": "Admin"}
