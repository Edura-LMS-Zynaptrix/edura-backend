"""user_service's queue: new accounts (to create their profile), account changes, enrollments (referral credit)."""

import logging

from database import SessionLocal
from models import ReplicaAccount
from profile_service import announce_everything, create_profile
from referrals import credit_on_enrollment
from schemas import UserProfileCreate

from shared import events as ev
from shared import messaging, replicas

logger = logging.getLogger(__name__)

QUEUE = "user.queue"


def handle(key: str, data: dict) -> None:
    db = SessionLocal()
    try:
        if key in (ev.AUTH_USER_REGISTERED, ev.AUTH_ACCOUNT_UPDATED):
            replicas.apply(db, key, data, account=ReplicaAccount)
            profile = data.get("profile")
            if key == ev.AUTH_USER_REGISTERED and profile:
                # The account and its sign-up details arrive together; build the profile.
                _, created = create_profile(
                    db,
                    int(data["user_id"]),
                    data.get("role") or "student",
                    UserProfileCreate(**profile),
                )
                if created:
                    logger.info("Created the profile for user %s", data["user_id"])
        elif key == ev.ENROLLMENT_ACTIVATED:
            student_id = data.get("student_id")
            # A manual grant by an admin is not a purchase, so it earns no referral credit.
            if (
                student_id
                and data.get("source") != "manual"
                and credit_on_enrollment(db, int(student_id))
            ):
                logger.info(
                    "Credited referral for first enrollment of user %s", student_id
                )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def start() -> None:
    messaging.start_consumer("user", QUEUE, ev.QUEUES[QUEUE], handle)


def resync() -> None:
    db = SessionLocal()
    try:
        announce_everything(db)
    finally:
        db.close()
