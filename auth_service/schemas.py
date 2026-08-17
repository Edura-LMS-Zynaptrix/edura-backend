from typing import Optional
from pydantic import BaseModel, EmailStr, Field
from models import UserRole, OtpPurpose


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


class UserResponse(BaseModel):
    id: int
    email: str
    role: UserRole
    is_active: bool
    is_email_verified: bool

    model_config = {"from_attributes": True}


class ErrorResponse(BaseModel):
    error: str
    message: str
