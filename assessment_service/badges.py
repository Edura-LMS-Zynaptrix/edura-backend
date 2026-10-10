"""
Badges: the reference platform's ten achievements, evaluated after each
graded attempt. Everything they depend on is attempt history, which this
service owns; only "Junior Scholar" needs the account age from auth_service.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from models import Assessment, Badge, Submission, SubmissionStatus, UserBadge
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth_service:8001")
# Time-of-day badges use the students' local time (Sri Lanka, UTC+05:30).
LOCAL_OFFSET = timedelta(minutes=int(os.getenv("BADGE_TZ_OFFSET_MINUTES", "330")))
PASS_PERCENT = 85.0

BADGE_DEFINITIONS = (
    ("early_bird", "Early Bird", "Finished a paper before 6 AM.", "activity", "Sunrise"),
    ("night_owl", "Night Owl", "Finished a paper late at night.", "activity", "Moon"),
    ("consistency_king", "Consistency King", "Wrote a paper every day for a week.", "activity", "CalendarHeart"),
    ("perfect_100", "Perfect 100", "Scored 100% on a paper on the first attempt.", "performance", "Trophy"),
    ("speed_demon", "Speed Demon", "Scored over 85% in under a third of the allowed time.", "performance", "Zap"),
    ("improved_star", "Improved Star", "Beat your previous best score on a paper.", "performance", "TrendingUp"),
    ("subject_expert", "Subject Expert", "Passed 5 papers in one lesson on the first attempt.", "performance", "Library"),
    ("first_step", "First Step", "Finished your first paper with over 85%.", "milestone", "Footprints"),
    ("century_club", "Century Club", "Finished 100 papers.", "milestone", "Target"),
    ("junior_scholar", "Junior Scholar", "Stayed active for 4 weeks.", "milestone", "GraduationCap"),
)


def seed_badges(db: Session) -> None:
    existing = {key for (key,) in db.query(Badge.criteria_key).all()}
    for key, name, description, category, icon in BADGE_DEFINITIONS:
        if key not in existing:
            db.add(Badge(criteria_key=key, name=name, description=description, category=category, icon_name=icon))
    db.commit()


def _local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc) + LOCAL_OFFSET


def _account_created_at(student_id: int) -> datetime | None:
    try:
        r = httpx.get(f"{AUTH_SERVICE_URL}/internal/users/{student_id}", timeout=5.0)
        if r.status_code != 200:
            return None
        created = datetime.fromisoformat(r.json()["created_at"].replace("Z", "+00:00"))
        return created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    except Exception as e:
        logger.warning("Could not read account age for %s: %s", student_id, e)
        return None


def _award(db: Session, student_id: int, key: str, owned: set[int]) -> Badge | None:
    badge = db.query(Badge).filter(Badge.criteria_key == key).first()
    if not badge or badge.id in owned:
        return None
    db.add(UserBadge(student_id=student_id, badge_id=badge.id))
    try:
        db.commit()
    except IntegrityError:  # awarded concurrently
        db.rollback()
        return None
    owned.add(badge.id)
    return badge


def evaluate_after_attempt(db: Session, submission: Submission, assessment: Assessment) -> list[Badge]:
    """Award every badge this attempt newly qualifies for; returns only the new ones."""
    student_id = submission.student_id
    owned = {b for (b,) in db.query(UserBadge.badge_id).filter(UserBadge.student_id == student_id).all()}
    history = (
        db.query(Submission)
        .filter(Submission.student_id == student_id, Submission.status == SubmissionStatus.graded)
        .order_by(Submission.submitted_at, Submission.id)
        .all()
    )
    score = float(submission.score or 0)
    earned: list[str] = []

    started = _local(submission.started_at)
    submitted = _local(submission.submitted_at)

    # Activity
    if 4 <= started.hour < 6:
        earned.append("early_bird")
    if submitted.hour >= 22 or submitted.hour < 2:
        earned.append("night_owl")
    week_ago = datetime.now(timezone.utc) + LOCAL_OFFSET - timedelta(days=7)
    recent_days = {_local(s.submitted_at).date() for s in history if s.submitted_at and _local(s.submitted_at) >= week_ago}
    if len(recent_days) >= 7:
        earned.append("consistency_king")

    # Performance
    same_exam = [s for s in history if s.assessment_id == assessment.id]
    is_first_attempt = bool(same_exam) and same_exam[0].id == submission.id
    if score >= 100 and is_first_attempt:
        earned.append("perfect_100")

    allowed_seconds = (assessment.time_limit_minutes or 60) * 60
    taken = (submission.submitted_at - submission.started_at).total_seconds()
    if score >= PASS_PERCENT and taken < allowed_seconds / 3:
        earned.append("speed_demon")

    previous = [float(s.score or 0) for s in same_exam if s.id != submission.id]
    if previous and score > max(previous):
        earned.append("improved_star")

    if assessment.module_id is not None:
        module_exam_ids = {
            a_id for (a_id,) in db.query(Assessment.id).filter(Assessment.module_id == assessment.module_id).all()
        }
        first_scores: dict[int, float] = {}
        for s in history:
            if s.assessment_id in module_exam_ids and s.assessment_id not in first_scores:
                first_scores[s.assessment_id] = float(s.score or 0)
        if sum(1 for v in first_scores.values() if v >= PASS_PERCENT) >= 5:
            earned.append("subject_expert")

    # Milestones
    if score >= PASS_PERCENT and len(history) == 1:
        earned.append("first_step")
    if len(history) >= 100:
        earned.append("century_club")
    weeks_active = {_local(s.submitted_at).isocalendar()[:2] for s in history if s.submitted_at}
    if len(weeks_active) >= 4 and "junior_scholar" not in earned:
        created = _account_created_at(student_id)
        if created and datetime.now(timezone.utc) - created >= timedelta(weeks=4):
            earned.append("junior_scholar")

    new_badges = []
    for key in earned:
        badge = _award(db, student_id, key, owned)
        if badge:
            new_badges.append(badge)
    return new_badges
