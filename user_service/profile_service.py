"""Creating and announcing profiles. Shared by the HTTP endpoints and the event consumer."""

import secrets
import string

from models import ProfileRole, Stream, UserProfile
from referrals import bind_referral, find_referrer
from schemas import UserProfileCreate
from sqlalchemy.orm import Session

from shared import events as ev
from shared.messaging import publish


def generate_referral_code(db: Session, first_name: str) -> str:
    """Short, human-shareable code like `JOHN4F2A` — collision-checked
    against the (unique) referral_code column with a handful of retries."""
    prefix = "".join(ch for ch in first_name.upper() if ch.isalpha())[:4] or "USER"
    for _ in range(10):
        suffix = "".join(
            secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4)
        )
        code = f"{prefix}{suffix}"
        if not db.query(UserProfile).filter(UserProfile.referral_code == code).first():
            return code
    return secrets.token_hex(6).upper()


def profile_event(profile: UserProfile) -> dict:
    """The profile as other services keep it (names and the details they filter on)."""
    return {
        "user_id": profile.user_id,
        "role": profile.role.value,
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "full_name": f"{profile.first_name} {profile.last_name}".strip(),
        "mobile_no": profile.mobile_no,
        "school": profile.school,
        "district": profile.district,
        "grade": profile.grade,
        "stream_id": profile.stream_id,
        "avatar_url": profile.avatar_url,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
    }


def announce_profile(profile: UserProfile) -> None:
    publish(ev.USER_PROFILE_UPSERTED, profile_event(profile))


def announce_profile_deleted(user_id: int) -> None:
    publish(ev.USER_PROFILE_DELETED, {"user_id": user_id})


def stream_event(stream: Stream) -> dict:
    return {
        "id": stream.id,
        "name": stream.name,
        "description": stream.description,
        "subjects": stream.subjects or [],
        "is_active": stream.is_active,
    }


def announce_stream(stream: Stream) -> None:
    publish(ev.USER_STREAM_UPSERTED, stream_event(stream))


def create_profile(
    db: Session, user_id: int, role: str, body: UserProfileCreate
) -> tuple[UserProfile, bool]:
    """Create the profile for an account. Returns (profile, created).

    Safe to repeat: when the account already has one, that one is returned
    unchanged (an event can be delivered twice). Unknown referral codes are ignored.
    """
    existing = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if existing:
        return existing, False

    referrer = find_referrer(db, body.referral_code)
    profile = UserProfile(
        user_id=user_id,
        role=ProfileRole(role),
        first_name=body.first_name,
        last_name=body.last_name,
        mobile_no=body.mobile_no,
        date_of_birth=body.date_of_birth,
        bio=body.bio,
        avatar_url=body.avatar_url,
        avatar_public_id=body.avatar_public_id,
        school=body.school,
        district=body.district,
        grade=body.grade,
        stream_id=body.stream_id,
        nic_number=body.nic_number,
        selected_subjects=body.selected_subjects,
        referral_code=generate_referral_code(db, body.first_name),
    )
    db.add(profile)
    if referrer:
        bind_referral(db, profile, referrer)
    db.commit()
    db.refresh(profile)
    announce_profile(profile)
    return profile, True


def announce_everything(db: Session) -> None:
    """Re-announce every stream and profile, so a service with an empty replica catches up."""
    for stream in db.query(Stream).all():
        announce_stream(stream)
    for profile in db.query(UserProfile).all():
        announce_profile(profile)
