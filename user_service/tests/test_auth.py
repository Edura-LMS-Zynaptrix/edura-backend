"""
Unit tests for shared/auth.py — require_role dependency.

These tests are fully in-memory: no database, no external services.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jose import jwt

# Set secret before importing require_role so the module picks it up
os.environ["SECRET_KEY"] = "test-secret-key-for-unit-tests"

from shared.auth import require_role  # noqa: E402

ALGORITHM = "HS256"
SECRET = "test-secret-key-for-unit-tests"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_token(role: str, exp_seconds: int = 3600, sub: str = "42") -> str:
    payload = {
        "sub": sub,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=exp_seconds),
    }
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Minimal FastAPI app wired with require_role
# ---------------------------------------------------------------------------

_app = FastAPI()


@_app.get("/admin-only")
async def admin_only(_: dict = Depends(require_role(["admin"]))):
    return {"ok": True}


@_app.get("/teacher-or-admin")
async def teacher_or_admin(_: dict = Depends(require_role(["teacher", "admin"]))):
    return {"ok": True}


@_app.get("/any-role")
async def any_role(payload: dict = Depends(require_role(["student", "teacher", "admin"]))):
    return {"sub": payload["sub"], "role": payload["role"]}


_client = TestClient(_app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# AC4: require_role(["TEACHER","ADMIN"]) — blocks STUDENT
# ---------------------------------------------------------------------------

class TestAdminOnly:
    def test_admin_passes(self):
        r = _client.get("/admin-only", headers=_auth(_make_token("admin")))
        assert r.status_code == 200

    def test_student_blocked(self):
        r = _client.get("/admin-only", headers=_auth(_make_token("student")))
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"

    def test_teacher_blocked(self):
        r = _client.get("/admin-only", headers=_auth(_make_token("teacher")))
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


class TestTeacherOrAdmin:
    def test_teacher_passes(self):
        r = _client.get("/teacher-or-admin", headers=_auth(_make_token("teacher")))
        assert r.status_code == 200

    def test_admin_passes(self):
        r = _client.get("/teacher-or-admin", headers=_auth(_make_token("admin")))
        assert r.status_code == 200

    def test_student_blocked(self):
        r = _client.get("/teacher-or-admin", headers=_auth(_make_token("student")))
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


# ---------------------------------------------------------------------------
# AC: missing / malformed Authorization header → 401 MISSING_TOKEN
# ---------------------------------------------------------------------------

class TestMissingToken:
    def test_no_header_returns_401(self):
        r = _client.get("/admin-only")
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "MISSING_TOKEN"

    def test_token_without_bearer_prefix_returns_401(self):
        r = _client.get("/admin-only", headers={"Authorization": "Token abc123"})
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "MISSING_TOKEN"

    def test_empty_bearer_returns_401(self):
        r = _client.get("/admin-only", headers={"Authorization": "Bearer "})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# AC: expired JWT → 401 TOKEN_EXPIRED
# ---------------------------------------------------------------------------

class TestExpiredToken:
    def test_expired_token_returns_401(self):
        token = _make_token("admin", exp_seconds=-60)
        r = _client.get("/admin-only", headers=_auth(token))
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "TOKEN_EXPIRED"


# ---------------------------------------------------------------------------
# AC: tampered / invalid token → 401 INVALID_TOKEN
# ---------------------------------------------------------------------------

class TestInvalidToken:
    def test_garbage_token_returns_401(self):
        r = _client.get("/admin-only", headers={"Authorization": "Bearer not.a.jwt"})
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "INVALID_TOKEN"

    def test_wrong_secret_returns_401(self):
        token = jwt.encode(
            {"sub": "1", "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            "wrong-secret",
            algorithm=ALGORITHM,
        )
        r = _client.get("/admin-only", headers=_auth(token))
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "INVALID_TOKEN"


# ---------------------------------------------------------------------------
# AC5: Depends injection returns payload to handler
# ---------------------------------------------------------------------------

class TestPayloadInjection:
    def test_payload_sub_and_role_accessible(self):
        token = _make_token("student", sub="99")
        r = _client.get("/any-role", headers=_auth(token))
        assert r.status_code == 200
        assert r.json()["sub"] == "99"
        assert r.json()["role"] == "student"
