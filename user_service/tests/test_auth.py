"""
Unit tests for shared/auth.py — require_role dependency.

These tests are fully in-memory: no database, no external services.
"""

import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

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
async def any_role(
    payload: dict = Depends(require_role(["student", "teacher", "admin"]))
):
    return {"sub": payload["sub"], "role": payload["role"]}


_client = TestClient(_app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# AC4: require_role(["TEACHER","ADMIN"]) — blocks STUDENT
# ---------------------------------------------------------------------------


class TestAdminOnly:
    def test_given_admin_token_when_admin_only_route_called_then_200(self):
        # Act
        r = _client.get("/admin-only", headers=_auth(_make_token("admin")))

        # Assert
        assert r.status_code == 200

    def test_given_student_token_when_admin_only_route_called_then_403_insufficient_permissions(
        self,
    ):
        # Act
        r = _client.get("/admin-only", headers=_auth(_make_token("student")))

        # Assert
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"

    def test_given_teacher_token_when_admin_only_route_called_then_403_insufficient_permissions(
        self,
    ):
        # Act
        r = _client.get("/admin-only", headers=_auth(_make_token("teacher")))

        # Assert
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


class TestTeacherOrAdmin:
    def test_given_teacher_token_when_teacher_or_admin_route_called_then_200(self):
        # Act
        r = _client.get("/teacher-or-admin", headers=_auth(_make_token("teacher")))

        # Assert
        assert r.status_code == 200

    def test_given_admin_token_when_teacher_or_admin_route_called_then_200(self):
        # Act
        r = _client.get("/teacher-or-admin", headers=_auth(_make_token("admin")))

        # Assert
        assert r.status_code == 200

    def test_given_student_token_when_teacher_or_admin_route_called_then_403_insufficient_permissions(
        self,
    ):
        # Act
        r = _client.get("/teacher-or-admin", headers=_auth(_make_token("student")))

        # Assert
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"


# ---------------------------------------------------------------------------
# AC: missing / malformed Authorization header → 401 MISSING_TOKEN
# ---------------------------------------------------------------------------


class TestMissingToken:
    def test_given_no_authorization_header_when_route_called_then_401_missing_token(
        self,
    ):
        # Act
        r = _client.get("/admin-only")

        # Assert
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "MISSING_TOKEN"

    def test_given_token_without_bearer_prefix_when_route_called_then_401_missing_token(
        self,
    ):
        # Act
        r = _client.get("/admin-only", headers={"Authorization": "Token abc123"})

        # Assert
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "MISSING_TOKEN"

    def test_given_empty_bearer_value_when_route_called_then_401(self):
        # Act
        r = _client.get("/admin-only", headers={"Authorization": "Bearer "})

        # Assert
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# AC: expired JWT → 401 TOKEN_EXPIRED
# ---------------------------------------------------------------------------


class TestExpiredToken:
    def test_given_expired_token_when_route_called_then_401_token_expired(self):
        # Arrange
        token = _make_token("admin", exp_seconds=-60)

        # Act
        r = _client.get("/admin-only", headers=_auth(token))

        # Assert
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "TOKEN_EXPIRED"


# ---------------------------------------------------------------------------
# AC: tampered / invalid token → 401 INVALID_TOKEN
# ---------------------------------------------------------------------------


class TestInvalidToken:
    def test_given_garbage_token_when_route_called_then_401_invalid_token(self):
        # Act
        r = _client.get("/admin-only", headers={"Authorization": "Bearer not.a.jwt"})

        # Assert
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "INVALID_TOKEN"

    def test_given_token_signed_with_wrong_secret_when_route_called_then_401_invalid_token(
        self,
    ):
        # Arrange
        token = jwt.encode(
            {
                "sub": "1",
                "role": "admin",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            "wrong-secret",
            algorithm=ALGORITHM,
        )

        # Act
        r = _client.get("/admin-only", headers=_auth(token))

        # Assert
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "INVALID_TOKEN"


# ---------------------------------------------------------------------------
# AC5: Depends injection returns payload to handler
# ---------------------------------------------------------------------------


class TestPayloadInjection:
    def test_given_valid_token_when_route_called_then_handler_receives_sub_and_role(
        self,
    ):
        # Arrange
        token = _make_token("student", sub="99")

        # Act
        r = _client.get("/any-role", headers=_auth(token))

        # Assert
        assert r.status_code == 200
        assert r.json()["sub"] == "99"
        assert r.json()["role"] == "student"
