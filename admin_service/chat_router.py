"""
Support chat: students (or not-yet-signed-in guests) message the admin team.
A guest thread is keyed by a random browser session id and is attached to
the account by /chat/sync once that guest signs in.
"""

from datetime import datetime, timezone

from database import get_db
from fastapi import APIRouter, Depends, HTTPException, status
from models import ChatConversation, ChatMessage
from schemas import (
    AdminChatReply,
    ChatConversationResponse,
    ChatMessageCreate,
    ChatMessageResponse,
    ChatSyncRequest,
)
from sqlalchemy import func
from sqlalchemy.orm import Session
from user_client import display_names

from shared.auth import get_optional_payload, require_role

router = APIRouter(prefix="/chat")

_ADMIN = ["admin"]
_ANY_ROLE = ["student", "teacher", "admin"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conversation_for(db: Session, user_id: int | None, session_id: str | None) -> ChatConversation | None:
    if user_id is not None:
        conv = db.query(ChatConversation).filter(ChatConversation.user_id == user_id).first()
        if conv:
            return conv
    if session_id:
        conv = db.query(ChatConversation).filter(ChatConversation.session_id == session_id).first()
        # A signed-in user may only use a guest thread nobody has claimed yet.
        if conv and (conv.user_id is None or conv.user_id == user_id):
            return conv
    return None


def _get_conversation_or_404(conversation_id: int, db: Session) -> ChatConversation:
    conv = db.query(ChatConversation).filter(ChatConversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail={"error": "CONVERSATION_NOT_FOUND"})
    return conv


async def _describe(conversations: list[ChatConversation], db: Session) -> list[ChatConversationResponse]:
    if not conversations:
        return []
    ids = [c.id for c in conversations]
    unread = dict(
        db.query(ChatMessage.conversation_id, func.count(ChatMessage.id))
        .filter(
            ChatMessage.conversation_id.in_(ids),
            ChatMessage.is_from_admin.is_(False),
            ChatMessage.is_read.is_(False),
        )
        .group_by(ChatMessage.conversation_id)
        .all()
    )
    latest: dict[int, str] = {}
    for msg in (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id.in_(ids))
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .all()
    ):
        latest.setdefault(msg.conversation_id, msg.message)
    names = await display_names([c.user_id for c in conversations if c.user_id is not None])
    return [
        ChatConversationResponse(
            id=c.id,
            session_id=c.session_id,
            user_id=c.user_id,
            is_active=c.is_active,
            last_message_at=c.last_message_at,
            created_at=c.created_at,
            last_message=latest.get(c.id),
            unread_count=unread.get(c.id, 0),
            student_name=names.get(c.user_id, "Student") if c.user_id is not None else "Guest",
        )
        for c in conversations
    ]


# ---------------------------------------------------------------------------
# Student / guest side
# ---------------------------------------------------------------------------


@router.post("/send", response_model=ChatMessageResponse)
async def send_message(
    body: ChatMessageCreate,
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    user_id = int(payload["sub"]) if payload else None
    if user_id is None and not body.session_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "IDENTITY_REQUIRED", "message": "Sign in or provide a session_id"},
        )
    conv = _conversation_for(db, user_id, body.session_id)
    if conv is None:
        conv = ChatConversation(user_id=user_id, session_id=None if user_id else body.session_id)
        db.add(conv)
        db.flush()
    msg = ChatMessage(conversation_id=conv.id, message=body.message.strip(), sender_id=user_id)
    db.add(msg)
    conv.last_message_at = _now()
    db.commit()
    db.refresh(msg)
    return msg


@router.get("/history", response_model=list[ChatMessageResponse])
async def chat_history(
    session_id: str | None = None,
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    conv = _conversation_for(db, int(payload["sub"]) if payload else None, session_id)
    if conv is None:
        return []
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conv.id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .all()
    )


@router.post("/sync")
async def sync_session(
    body: ChatSyncRequest,
    payload: dict = Depends(require_role(_ANY_ROLE)),
    db: Session = Depends(get_db),
):
    """Attach a guest thread to the signed-in user, merging it into any thread they already had."""
    user_id = int(payload["sub"])
    guest = (
        db.query(ChatConversation)
        .filter(ChatConversation.session_id == body.session_id, ChatConversation.user_id.is_(None))
        .first()
    )
    if guest is None:
        return {"message": "Nothing to sync"}
    own = db.query(ChatConversation).filter(ChatConversation.user_id == user_id).first()
    if own is not None:
        db.query(ChatMessage).filter(ChatMessage.conversation_id == guest.id).update(
            {ChatMessage.conversation_id: own.id}
        )
        own.last_message_at = max(own.last_message_at, guest.last_message_at)
        db.delete(guest)
    else:
        guest.user_id = user_id
        guest.session_id = None
    db.commit()
    return {"message": "Chat session synced"}


# ---------------------------------------------------------------------------
# Admin side
# ---------------------------------------------------------------------------


@router.get("/admin/conversations", response_model=list[ChatConversationResponse])
async def admin_list_conversations(
    _: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    conversations = db.query(ChatConversation).order_by(ChatConversation.last_message_at.desc()).all()
    return await _describe(conversations, db)


@router.get("/admin/conversations/student/{user_id}", response_model=ChatConversationResponse)
async def admin_conversation_for_student(
    user_id: int,
    _: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    """Open (or start) the thread with one student, so an admin can message them first."""
    conv = db.query(ChatConversation).filter(ChatConversation.user_id == user_id).first()
    if conv is None:
        if not await display_names([user_id]):
            raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})
        conv = ChatConversation(user_id=user_id)
        db.add(conv)
        db.commit()
        db.refresh(conv)
    return (await _describe([conv], db))[0]


@router.get("/admin/conversations/{conversation_id}/messages", response_model=list[ChatMessageResponse])
async def admin_conversation_messages(
    conversation_id: int,
    _: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    conv = _get_conversation_or_404(conversation_id, db)
    db.query(ChatMessage).filter(
        ChatMessage.conversation_id == conv.id, ChatMessage.is_from_admin.is_(False)
    ).update({ChatMessage.is_read: True})
    db.commit()
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conv.id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .all()
    )


@router.post("/admin/conversations/{conversation_id}/reply", response_model=ChatMessageResponse)
async def admin_reply(
    conversation_id: int,
    body: AdminChatReply,
    payload: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    conv = _get_conversation_or_404(conversation_id, db)
    msg = ChatMessage(
        conversation_id=conv.id,
        message=body.message.strip(),
        is_from_admin=True,
        sender_id=int(payload["sub"]),
    )
    db.add(msg)
    conv.last_message_at = _now()
    db.commit()
    db.refresh(msg)
    return msg


@router.delete("/admin/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_conversation(
    conversation_id: int,
    _: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    conv = _get_conversation_or_404(conversation_id, db)
    db.query(ChatMessage).filter(ChatMessage.conversation_id == conv.id).delete()
    db.delete(conv)
    db.commit()
