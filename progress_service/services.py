from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from events import publish_certificate_issued
from models import (
    Certificate,
    CourseProgress,
    CourseProgressStatus,
    LeaderboardEntry,
    LessonProgress,
)
from redis_client import get_redis_client
from sqlalchemy import func
from sqlalchemy.orm import Session

# Memory/Cache store for student assessment results per course (if assessment_service isn't directly sharing DB)
# Stores dict: (student_id, course_id) -> dict of assessment_id -> {"score": float, "passed": bool}
_assessment_results_store: Dict[tuple, Dict[int, Dict[str, Any]]] = {}


def record_assessment_result(
    student_id: int, course_id: int, assessment_id: int, score: float, passed: bool
):
    key = (student_id, course_id)
    if key not in _assessment_results_store:
        _assessment_results_store[key] = {}
    _assessment_results_store[key][assessment_id] = {
        "score": score,
        "passed": passed,
    }


def get_assessment_results(student_id: int, course_id: int) -> List[Dict[str, Any]]:
    key = (student_id, course_id)
    if key not in _assessment_results_store:
        return []
    res = []
    for aid, data in _assessment_results_store[key].items():
        res.append(
            {
                "assessment_id": aid,
                "score": data["score"],
                "passed": data["passed"],
            }
        )
    return res


def calculate_total_points(db: Session, student_id: int, course_id: int) -> int:
    """
    Calculates total leaderboard points:
    - Sum of assessment scores (0-100 per assessment)
    - Bonus +10 for 100% course completion
    """
    assessment_list = get_assessment_results(student_id, course_id)
    assessment_points = sum(int(a["score"]) for a in assessment_list)

    cp = (
        db.query(CourseProgress)
        .filter(
            CourseProgress.student_id == student_id,
            CourseProgress.course_id == course_id,
        )
        .first()
    )

    bonus = 0
    if cp and float(cp.completion_percentage) >= 100.0:
        bonus = 10

    return assessment_points + bonus


def update_leaderboard(db: Session, student_id: int, course_id: int):
    """
    Updates LeaderboardEntry in database and Redis sorted set 'leaderboard:course:{course_id}'
    """
    total_pts = calculate_total_points(db, student_id, course_id)

    # DB record update
    entry = (
        db.query(LeaderboardEntry)
        .filter(
            LeaderboardEntry.student_id == student_id,
            LeaderboardEntry.course_id == course_id,
        )
        .first()
    )

    if not entry:
        entry = LeaderboardEntry(
            student_id=student_id,
            course_id=course_id,
            points=total_pts,
            last_activity_at=datetime.now(timezone.utc),
        )
        db.add(entry)
    else:
        entry.points = total_pts
        entry.last_activity_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(entry)

    # Redis sorted set update
    redis_c = get_redis_client()
    redis_key = f"leaderboard:course:{course_id}"
    redis_c.zadd(redis_key, {str(student_id): total_pts})


def process_lesson_watched(
    db: Session,
    student_id: int,
    course_id: int,
    lesson_id: int,
    total_lessons: Optional[int] = None,
    watch_duration: int = 0,
    last_position: int = 0,
) -> LessonProgress:
    now = datetime.now(timezone.utc)

    # 1. Update/Create LessonProgress
    lp = (
        db.query(LessonProgress)
        .filter(
            LessonProgress.student_id == student_id,
            LessonProgress.lesson_id == lesson_id,
        )
        .first()
    )

    if not lp:
        lp = LessonProgress(
            student_id=student_id,
            course_id=course_id,
            lesson_id=lesson_id,
            is_completed=True,
            watch_duration_seconds=watch_duration,
            last_position_seconds=last_position,
            completed_at=now,
        )
        db.add(lp)
    else:
        lp.is_completed = True
        lp.completed_at = now
        lp.watch_duration_seconds = max(lp.watch_duration_seconds, watch_duration)
        lp.last_position_seconds = last_position

    db.commit()
    db.refresh(lp)

    # 2. Recalculate CourseProgress
    watched_count = (
        db.query(func.count(LessonProgress.id))
        .filter(
            LessonProgress.student_id == student_id,
            LessonProgress.course_id == course_id,
            LessonProgress.is_completed == True,  # noqa: E712
        )
        .scalar()
        or 0
    )

    cp = (
        db.query(CourseProgress)
        .filter(
            CourseProgress.student_id == student_id,
            CourseProgress.course_id == course_id,
        )
        .first()
    )

    if not cp:
        tot = total_lessons if total_lessons is not None and total_lessons > 0 else 1
        pct = round((watched_count / tot) * 100, 2)
        cp = CourseProgress(
            student_id=student_id,
            course_id=course_id,
            completed_lessons_count=watched_count,
            total_lessons_count=tot,
            completion_percentage=pct,
            status=(
                CourseProgressStatus.completed
                if pct >= 100.0
                else CourseProgressStatus.in_progress
            ),
            completed_at=now if pct >= 100.0 else None,
        )
        db.add(cp)
    else:
        if total_lessons is not None and total_lessons > 0:
            cp.total_lessons_count = total_lessons

        tot = cp.total_lessons_count if cp.total_lessons_count > 0 else 1
        pct = round((watched_count / tot) * 100, 2)
        cp.completed_lessons_count = watched_count
        cp.completion_percentage = pct

        if pct >= 100.0:
            cp.status = CourseProgressStatus.completed
            if not cp.completed_at:
                cp.completed_at = now
        else:
            cp.status = CourseProgressStatus.in_progress

    db.commit()
    db.refresh(cp)

    # 3. Update leaderboard points
    update_leaderboard(db, student_id, course_id)

    # 4. Check certificate eligibility
    check_certificate_eligibility(db, student_id, course_id)

    return lp


def process_assessment_graded(
    db: Session,
    student_id: int,
    course_id: int,
    assessment_id: int,
    score: float,
    passed: bool,
):
    # Store assessment result
    record_assessment_result(student_id, course_id, assessment_id, score, passed)

    # Update leaderboard
    update_leaderboard(db, student_id, course_id)

    # Check certificate eligibility
    check_certificate_eligibility(db, student_id, course_id)


def check_certificate_eligibility(
    db: Session, student_id: int, course_id: int
) -> Optional[Certificate]:
    """
    BR-004: Certification eligibility requires 100% video completion AND all assessments passed.
    """
    cp = (
        db.query(CourseProgress)
        .filter(
            CourseProgress.student_id == student_id,
            CourseProgress.course_id == course_id,
        )
        .first()
    )

    if not cp or float(cp.completion_percentage) < 100.0:
        return None

    assessments = get_assessment_results(student_id, course_id)
    if assessments:
        # Check if all assessments recorded have passed=True
        all_passed = all(a["passed"] for a in assessments)
        if not all_passed:
            return None

    # Check if certificate already exists
    cert = (
        db.query(Certificate)
        .filter(
            Certificate.student_id == student_id,
            Certificate.course_id == course_id,
        )
        .first()
    )

    if cert:
        return cert

    now = datetime.now(timezone.utc)
    cert = Certificate(
        student_id=student_id,
        course_id=course_id,
        certificate_url=f"/certificates/{student_id}_{course_id}.pdf",
        issued_at=now,
    )
    db.add(cert)
    db.commit()
    db.refresh(cert)

    # Publish certificate.issued event to RabbitMQ
    publish_certificate_issued(
        student_id=student_id,
        course_id=course_id,
        certificate_id=cert.id,
        issued_at=cert.issued_at,
    )

    return cert


def get_student_course_progress(
    db: Session, student_id: int, course_id: int
) -> Dict[str, Any]:
    cp = (
        db.query(CourseProgress)
        .filter(
            CourseProgress.student_id == student_id,
            CourseProgress.course_id == course_id,
        )
        .first()
    )

    cert = (
        db.query(Certificate)
        .filter(
            Certificate.student_id == student_id,
            Certificate.course_id == course_id,
        )
        .first()
    )

    completion_pct = float(cp.completion_percentage) if cp else 0.0
    watched_lessons = cp.completed_lessons_count if cp else 0
    total_lessons = cp.total_lessons_count if cp else 0

    scores = get_assessment_results(student_id, course_id)

    return {
        "completion_percentage": completion_pct,
        "watched_lessons": watched_lessons,
        "total_lessons": total_lessons,
        "assessment_scores": scores,
        "certificate_issued": cert is not None,
    }


def get_course_leaderboard(
    db: Session, course_id: int, limit: int = 10
) -> List[Dict[str, Any]]:
    redis_c = get_redis_client()
    redis_key = f"leaderboard:course:{course_id}"

    # Try fetching from Redis sorted set
    top_entries = redis_c.zrevrange(redis_key, 0, limit - 1, withscores=True)

    result = []
    if top_entries:
        for rank, (student_id_str, score) in enumerate(top_entries, start=1):
            result.append(
                {
                    "rank": rank,
                    "student_id": int(student_id_str),
                    "points": int(score),
                }
            )
        return result

    # Fallback to DB query if Redis has no data
    db_entries = (
        db.query(LeaderboardEntry)
        .filter(LeaderboardEntry.course_id == course_id)
        .order_by(LeaderboardEntry.points.desc())
        .limit(limit)
        .all()
    )

    for rank, entry in enumerate(db_entries, start=1):
        result.append(
            {
                "rank": rank,
                "student_id": entry.student_id,
                "points": entry.points,
            }
        )

    return result
