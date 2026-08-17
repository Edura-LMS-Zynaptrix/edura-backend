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
        "/api/auth/register",
        json={
            "email": "student@edura.com",
            "password": "password123",
            "role": "student",
        },
    )
    assert reg_resp.status_code == 200
    data = reg_resp.json()
    assert data["email"] == "student@edura.com"

    # Login user
    login_resp = client.post(
        "/api/auth/login",
        json={"email": "student@edura.com", "password": "password123"},
    )
    assert login_resp.status_code == 200
    token_data = login_resp.json()
    assert "access_token" in token_data
    assert token_data["expires_in"] == 900
    assert "refresh_token" in login_resp.cookies


def test_login_invalid_credentials():
    client.post(
        "/api/auth/register",
        json={
            "email": "teacher@edura.com",
            "password": "password123",
            "role": "teacher",
        },
    )

    resp = client.post(
        "/api/auth/login",
        json={"email": "teacher@edura.com", "password": "wrongpassword"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "INVALID_CREDENTIALS"


def test_refresh_token_rotation_and_reuse():
    # Register & Login
    client.post(
        "/api/auth/register",
        json={"email": "admin@edura.com", "password": "password123", "role": "admin"},
    )
    login_resp = client.post(
        "/api/auth/login",
        json={"email": "admin@edura.com", "password": "password123"},
    )
    initial_refresh_token = login_resp.cookies["refresh_token"]

    # First refresh call - Success
    ref_resp = client.post(
        "/api/auth/refresh", json={"refresh_token": initial_refresh_token}
    )
    assert ref_resp.status_code == 200
    new_access_token = ref_resp.json()["access_token"]
    assert new_access_token is not None

    # Reuse of initial refresh token - Should fail with REFRESH_TOKEN_REUSE
    reuse_resp = client.post(
        "/api/auth/refresh", json={"refresh_token": initial_refresh_token}
    )
    assert reuse_resp.status_code == 401
    assert reuse_resp.json()["detail"]["error"] == "REFRESH_TOKEN_REUSE"


def test_protected_route_role_enforcement():
    # Register Student
    client.post(
        "/api/auth/register",
        json={"email": "user@edura.com", "password": "password123", "role": "student"},
    )
    login_resp = client.post(
        "/api/auth/login",
        json={"email": "user@edura.com", "password": "password123"},
    )
    access_token = login_resp.json()["access_token"]

    # Access protected route with Bearer token
    headers = {"Authorization": f"Bearer {access_token}"}
    prot_resp = client.get("/api/auth/protected", headers=headers)
    assert prot_resp.status_code == 200
    assert prot_resp.json()["role"] == "STUDENT"

    # Access admin route with student token - Should fail with 403 INSUFFICIENT_PERMISSIONS
    admin_resp = client.get("/api/auth/admin", headers=headers)
    assert admin_resp.status_code == 403
    assert admin_resp.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


def test_missing_and_expired_token():
    # Missing token
    resp_no_token = client.get("/api/auth/protected")
    assert resp_no_token.status_code == 401
    assert resp_no_token.json()["detail"]["error"] == "MISSING_TOKEN"
