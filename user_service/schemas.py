from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, field_validator


class UserProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    mobile_no: Optional[str] = None
    date_of_birth: Optional[date] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None


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
    created_at: datetime

    model_config = {"from_attributes": True}


class PaginatedUsersResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[UserProfileResponse]
