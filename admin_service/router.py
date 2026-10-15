from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Query, status
from models import Feedback
from schemas import FeedbackCreate, FeedbackResponse
from sqlalchemy.orm import Session

from shared.auth import get_optional_payload, require_role

router = APIRouter()

_ADMIN = ["admin"]


@router.get("/admin")
async def admin_only(_: None = Depends(require_role(_ADMIN))):
    return {"message": "Welcome Admin"}


# ---------------------------------------------------------------------------
# Feedback — public submit, admin triage
# ---------------------------------------------------------------------------


def _get_feedback_or_404(feedback_id: int, db: Session) -> Feedback:
    feedback = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if not feedback:
        raise HTTPException(status_code=404, detail={"error": "FEEDBACK_NOT_FOUND"})
    return feedback


@router.post("/feedback/submit", response_model=FeedbackResponse, status_code=201)
async def submit_feedback(
    body: FeedbackCreate,
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    """Anyone may send feedback; a signed-in sender is linked by their user id."""
    feedback = Feedback(
        user_id=int(payload["sub"]) if payload else None,
        name=body.name.strip(),
        email=body.email,
        category=body.category,
        subject=(body.subject or "").strip() or None,
        message=body.message.strip(),
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return feedback


@router.get("/feedback/admin/all", response_model=list[FeedbackResponse])
async def list_feedback(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    _: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    return (
        db.query(Feedback)
        .order_by(Feedback.created_at.desc(), Feedback.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.patch("/feedback/admin/{feedback_id}/read", response_model=FeedbackResponse)
async def mark_feedback_read(
    feedback_id: int,
    _: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    feedback = _get_feedback_or_404(feedback_id, db)
    feedback.is_read = True
    db.commit()
    db.refresh(feedback)
    return feedback


@router.delete("/feedback/admin/{feedback_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback(
    feedback_id: int,
    _: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    db.delete(_get_feedback_or_404(feedback_id, db))
    db.commit()
