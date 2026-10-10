"""
Referrals: every profile has a shareable code. Applying someone's code
creates a pending ReferralEvent; the referrer's wallet is credited when the
referred student first enrolls in a course (enrollment.activated event).
"""

import os
from datetime import datetime, timezone

from fastapi import HTTPException
from models import ReferralEvent, UserProfile, WalletTransaction
from sqlalchemy.orm import Session

CREDIT_AMOUNT = int(os.getenv("REFERRAL_CREDIT_AMOUNT", "1"))
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


def find_referrer(db: Session, code: str | None) -> UserProfile | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    return db.query(UserProfile).filter(UserProfile.referral_code == code).first()


def bind_referral(db: Session, profile: UserProfile, referrer: UserProfile) -> ReferralEvent | None:
    """Link profile to its referrer once and open a pending event. Caller commits."""
    if referrer.user_id == profile.user_id or profile.referred_by_user_id is not None:
        return None
    if db.query(ReferralEvent).filter(ReferralEvent.referred_user_id == profile.user_id).first():
        return None
    profile.referred_by_user_id = referrer.user_id
    event = ReferralEvent(
        referrer_user_id=referrer.user_id,
        referred_user_id=profile.user_id,
        status="pending",
        credit_amount=CREDIT_AMOUNT,
        qualified_at=datetime.now(timezone.utc),
        note="Pending — credited when the referred student first enrolls in a course",
    )
    db.add(event)
    return event


def apply_code(db: Session, profile: UserProfile, code: str) -> dict:
    """Explicit 'apply a referral code' action, with the reference platform's error cases."""
    if profile.role.value != "student":
        raise HTTPException(status_code=403, detail={"error": "STUDENTS_ONLY", "message": "Only students can apply referral codes"})
    if not (code or "").strip():
        raise HTTPException(status_code=400, detail={"error": "CODE_REQUIRED", "message": "Referral code is required"})
    if profile.referred_by_user_id is not None:
        raise HTTPException(status_code=400, detail={"error": "ALREADY_REFERRED", "message": "Referral already applied"})
    referrer = find_referrer(db, code)
    if not referrer:
        raise HTTPException(status_code=404, detail={"error": "INVALID_CODE", "message": "Invalid referral code"})
    if referrer.user_id == profile.user_id:
        raise HTTPException(status_code=400, detail={"error": "SELF_REFERRAL", "message": "You cannot refer yourself"})
    if not bind_referral(db, profile, referrer):
        raise HTTPException(status_code=400, detail={"error": "ALREADY_PROCESSED", "message": "Referral already processed"})
    db.commit()
    return {
        "message": "Referral code applied. Credit will be awarded when you enroll in a course.",
        "referrer_user_id": referrer.user_id,
    }


def credit_on_enrollment(db: Session, referred_user_id: int) -> bool:
    """Credit the referrer for this student's first enrollment; idempotent."""
    event = (
        db.query(ReferralEvent)
        .filter(ReferralEvent.referred_user_id == referred_user_id, ReferralEvent.status == "pending")
        .with_for_update()
        .first()
    )
    if not event:
        return False
    referrer = (
        db.query(UserProfile).filter(UserProfile.user_id == event.referrer_user_id).with_for_update().first()
    )
    if not referrer:
        return False

    referrer.wallet_credits += event.credit_amount
    referrer.total_credits_earned += event.credit_amount
    referrer.total_successful_referrals += 1
    event.status = "credited"
    event.credited_at = datetime.now(timezone.utc)
    event.note = "Credited on first course enrollment"
    db.add(
        WalletTransaction(
            user_id=referrer.user_id,
            transaction_type="referral_reward",
            amount=event.credit_amount,
            balance_after=referrer.wallet_credits,
            reference_id=event.id,
            description=f"Referral reward for user {referred_user_id}",
        )
    )
    db.commit()
    return True


def summary(profile: UserProfile) -> dict:
    return {
        "referral_code": profile.referral_code,
        "referral_link": f"{FRONTEND_URL.rstrip('/')}/register?ref={profile.referral_code}",
        "wallet_credits": profile.wallet_credits,
        "total_credits_earned": profile.total_credits_earned,
        "total_successful_referrals": profile.total_successful_referrals,
    }


def events_for(db: Session, referrer_user_id: int, limit: int) -> dict:
    rows = (
        db.query(ReferralEvent, UserProfile)
        .join(UserProfile, UserProfile.user_id == ReferralEvent.referred_user_id)
        .filter(ReferralEvent.referrer_user_id == referrer_user_id)
        .order_by(ReferralEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "events": [
            {
                "event_id": str(e.id),
                "referred_student_id": str(p.user_id),
                "referred_student_name": f"{p.first_name} {p.last_name}".strip(),
                "referred_student_school": p.school,
                "credit_amount": e.credit_amount,
                "status": e.status,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "credited_at": e.credited_at.isoformat() if e.credited_at else None,
            }
            for e, p in rows
        ]
    }
