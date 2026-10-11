import re
from datetime import datetime
from typing import Optional

from models import OtpPurpose, UserRole
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


def check_password_strength(password: str) -> str:
    """Same rule the registration form shows: 8+ chars, upper, lower, digit, special."""
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long")
    if not re.search(r"[A-Z]", password):
        raise ValueError("Password must contain at least one uppercase letter")
    if not re.search(r"[a-z]", password):
        raise ValueError("Password must contain at least one lowercase letter")
    if not re.search(r"\d", password):
        raise ValueError("Password must contain at least one number")
    if not re.search(r"[@$!%*?&#]", password):
        raise ValueError(
            "Password must contain at least one special character (@$!%*?&#)"
        )
    return password


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 900


class RefreshRequest(BaseModel):
    refresh_token: Optional[str] = None


class OtpRequest(BaseModel):
    user_id: int
    purpose: OtpPurpose = OtpPurpose.email_verify


class OtpVerifyRequest(BaseModel):
    user_id: int
    code: str
    purpose: OtpPurpose = OtpPurpose.email_verify


class UserRegisterRequest(BaseModel):
    email: EmailStr
    password: str
    role: UserRole = UserRole.student

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return check_password_strength(v)


class UserResponse(BaseModel):
    id: int
    email: str
    role: UserRole
    is_active: bool
    is_email_verified: bool

    model_config = {"from_attributes": True}


class CurrentUserResponse(BaseModel):
    id: int
    email: str
    role: UserRole
    is_active: bool
    is_email_verified: bool
    auth_provider: str
    has_password: bool

    model_config = {"from_attributes": True}


class ErrorResponse(BaseModel):
    error: str
    message: str


# ── Google Sign-In (implicit flow: the frontend sends an OAuth *access token*
# obtained via @react-oauth/google's useGoogleLogin({flow: 'implicit'}), which
# this service verifies by calling Google's userinfo endpoint) ──────────────


class GoogleLoginRequest(BaseModel):
    access_token: str


class SetPasswordRequest(BaseModel):
    """Let a Google-only account (no password yet) add one, unlocking
    email+password login on the same account alongside Google Sign-In."""

    new_password: str = Field(..., min_length=8)

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return check_password_strength(v)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return check_password_strength(v)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)
    confirm_password: str

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return check_password_strength(v)

    @model_validator(mode="after")
    def passwords_match(self) -> "ResetPasswordRequest":
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


# ── Admin management (admin-only; never reachable from public /register) ────


class CreateAdminRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    first_name: str
    last_name: str


class UserStatusUpdateRequest(BaseModel):
    is_active: bool


class InternalUserResponse(BaseModel):
    """Bulk-listing shape for other services (e.g. user_service enriching an
    admin-facing profile list with the is_active/email fields that only
    auth_service owns) — served from an unauthenticated /internal/ route,
    matching the pattern already used for cross-service reads elsewhere
    (course_service's /owner, content_service's /internal/receipt-upload)."""

    id: int
    email: str
    role: UserRole
    is_active: bool
    is_email_verified: bool
    created_at: datetime

    model_config = {"from_attributes": True}
