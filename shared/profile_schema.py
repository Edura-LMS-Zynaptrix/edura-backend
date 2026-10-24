"""The profile a person fills in when they sign up. One definition for every service.

auth_service validates it before announcing a registration, and user_service
validates the same shape when a profile is created or edited, so the two can't
drift apart.
"""

import re
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

PHONE_REGEX = r"^\d{10}$"
NIC_REGEX = r"^([0-9]{9}[vVxX]|[0-9]{12})$"
DISTRICT_MIN_LENGTH = 2


def validate_phone(v: Optional[str]) -> Optional[str]:
    if not v:
        return v
    phone = re.sub(r"[\s\-\(\)]", "", v)
    if not re.match(PHONE_REGEX, phone):
        raise ValueError("Phone number must be exactly 10 digits (e.g., 0771234567)")
    return phone


def validate_district(v: Optional[str]) -> Optional[str]:
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
        return validate_phone(v)

    @field_validator("district")
    @classmethod
    def validate_district(cls, v: Optional[str]) -> Optional[str]:
        return validate_district(v)

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
