import re
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

PHONE_REGEX = r"^\d{10}$"
NIC_REGEX = r"^([0-9]{9}[vVxX]|[0-9]{12})$"
DISTRICT_MIN_LENGTH = 2


def _validate_phone(v: Optional[str]) -> Optional[str]:
    if not v:
        return v
    phone = re.sub(r"[\s\-\(\)]", "", v)
    if not re.match(PHONE_REGEX, phone):
        raise ValueError("Phone number must be exactly 10 digits (e.g., 0771234567)")
    return phone


def _validate_district(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    if len(v.strip()) < DISTRICT_MIN_LENGTH:
        raise ValueError("District is required")
    return v.strip()


class UserProfileCreate(BaseModel):
    first_name: str
    last_name: str
    mobile_no: Optional[str] = None
    date_of_birth: Optional[date] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None
    avatar_public_id: Optional[str] = None

    school: Optional[str] = Field(default=None, max_length=200)
    district: Optional[str] = None
    grade: Optional[int] = Field(default=None, ge=5, le=13)
    stream_id: Optional[int] = None
    nic_number: Optional[str] = None
    selected_subjects: Optional[list[str]] = None
    referral_code: Optional[str] = Field(
        default=None,
        max_length=32,
        description="A code entered by this user, referring them to an existing member",
    )

    @field_validator("mobile_no")
    @classmethod
    def validate_mobile(cls, v: Optional[str]) -> Optional[str]:
        return _validate_phone(v)

    @field_validator("district")
    @classmethod
    def validate_district(cls, v: Optional[str]) -> Optional[str]:
        return _validate_district(v)

    @model_validator(mode="after")
    def validate_nic_for_grade(self) -> "UserProfileCreate":
        if self.grade in (12, 13):
            if not self.nic_number:
                raise ValueError("NIC Number is mandatory for Grade 12 and 13 students")
            if not re.match(NIC_REGEX, self.nic_number):
                raise ValueError(
                    "Invalid NIC format. Format should be 9 digits + V/X or 12 digits."
                )
        return self


class UserProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    mobile_no: Optional[str] = None
    date_of_birth: Optional[date] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None
    avatar_public_id: Optional[str] = None

    school: Optional[str] = Field(default=None, max_length=200)
    district: Optional[str] = None
    grade: Optional[int] = Field(default=None, ge=5, le=13)
    stream_id: Optional[int] = None
    nic_number: Optional[str] = None
    selected_subjects: Optional[list[str]] = None
    # A code *entered* by this user (someone else's). Applied only if they
    # haven't been referred yet; it never replaces their own referral_code.
    referral_code: Optional[str] = Field(default=None, max_length=32)

    @field_validator("mobile_no")
    @classmethod
    def validate_mobile(cls, v: Optional[str]) -> Optional[str]:
        return _validate_phone(v)

    @field_validator("district")
    @classmethod
    def validate_district(cls, v: Optional[str]) -> Optional[str]:
        return _validate_district(v)


class RoleUpdateRequest(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"student", "teacher", "admin"}
        if v not in allowed:
            raise ValueError(f"role must be one of {sorted(allowed)}")
        return v


class UserProfileResponse(BaseModel):
    id: int
    name: str
    email: Optional[str] = None
    role: str
    first_name: str
    last_name: str
    mobile_no: Optional[str] = None
    date_of_birth: Optional[date] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None
    avatar_public_id: Optional[str] = None

    school: Optional[str] = None
    district: Optional[str] = None
    grade: Optional[int] = None
    stream_id: Optional[int] = None
    nic_number: Optional[str] = None
    selected_subjects: Optional[list[str]] = None
    referral_code: Optional[str] = None
    referred_by_user_id: Optional[int] = None

    # Only populated when the caller enriched the list from auth_service's
    # /internal/users (e.g. the admin listing) — auth_service, not this
    # service, is the source of truth for both fields.
    is_active: Optional[bool] = None

    created_at: datetime

    model_config = {"from_attributes": True}


class PaginatedUsersResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[UserProfileResponse]


class InternalProfileCreateRequest(UserProfileCreate):
    """Everything a self-service profile has, plus who it belongs to (sent by auth_service)."""

    user_id: int
    role: str


class RoleStatsResponse(BaseModel):
    total_students: int
    total_teachers: int
    total_admins: int


# ── Streams & Subjects (reference data used by the registration form) ───────


class StreamCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    subjects: list[str] = Field(default_factory=list)
    is_active: bool = True


class StreamUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    subjects: Optional[list[str]] = None
    is_active: Optional[bool] = None


class StreamResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    subjects: list[str]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class GradeSubjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    grade: int = Field(..., ge=5, le=11)
    is_active: bool = True


class GradeSubjectUpdate(BaseModel):
    name: Optional[str] = None
    grade: Optional[int] = Field(default=None, ge=5, le=11)
    is_active: Optional[bool] = None


class GradeSubjectResponse(BaseModel):
    id: int
    name: str
    grade: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ReferralApplyRequest(BaseModel):
    referral_code: str = Field(..., min_length=1, max_length=32)
