from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class AdminBase(BaseModel):
    pass


class AdminResponse(AdminBase):
    id: int

    model_config = {"from_attributes": True}


class FeedbackCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr
    category: Literal["suggestion", "bug", "other"]
    subject: Optional[str] = Field(default=None, max_length=200)
    message: str = Field(..., min_length=1, max_length=5000)


class FeedbackResponse(BaseModel):
    id: int
    user_id: Optional[int] = None
    name: str
    email: str
    category: str
    subject: Optional[str] = None
    message: str
    is_read: bool
    is_resolved: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# Browser-generated random ids (crypto.randomUUID) — never accept anything else.
_SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"


class ChatMessageCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = Field(default=None, pattern=_SESSION_ID_PATTERN)


class ChatSyncRequest(BaseModel):
    session_id: str = Field(..., pattern=_SESSION_ID_PATTERN)


class AdminChatReply(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class ChatMessageResponse(BaseModel):
    id: int
    conversation_id: int
    message: str
    is_from_admin: bool
    is_read: bool
    sender_id: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatConversationResponse(BaseModel):
    id: int
    session_id: Optional[str] = None
    user_id: Optional[int] = None
    is_active: bool
    last_message_at: datetime
    created_at: datetime
    last_message: Optional[str] = None
    unread_count: int = 0
    student_name: str = "Guest"

