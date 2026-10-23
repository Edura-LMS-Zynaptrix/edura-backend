"""Assessment events. Other services (progress, analytics, notifications) act on these."""

from datetime import datetime

from shared import events as ev
from shared.messaging import publish


def assessment_event(a) -> dict:
    return {
        "id": a.id,
        "course_id": a.course_id,
        "module_id": a.module_id,
        "title": a.title,
        "position": a.position or 0,
        "is_published": bool(a.is_published),
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def announce_assessment(a) -> None:
    publish(ev.ASSESSMENT_UPSERTED, assessment_event(a))


def announce_assessment_deleted(assessment_id: int) -> None:
    publish(ev.ASSESSMENT_DELETED, {"id": assessment_id})


def announce_badge(
    student_id: int, badge_key: str, awarded_at: datetime | None
) -> None:
    publish(
        ev.BADGE_AWARDED,
        {
            "student_id": student_id,
            "badge_key": badge_key,
            "awarded_at": awarded_at.isoformat() if awarded_at else None,
        },
    )


def publish_assessment_graded(
    student_id: int,
    assessment_id: int,
    score: float,
    passed: bool,
    course_id: int | None = None,
    module_id: int | None = None,
    submission_id: int | None = None,
    marks_obtained: int | None = None,
    total_questions: int | None = None,
    time_seconds: int | None = None,
    submitted_at: datetime | None = None,
):
    """An attempt was graded. Carries the whole result, so listeners need nothing else."""
    publish(
        ev.ASSESSMENT_GRADED,
        {
            "student_id": student_id,
            "assessment_id": assessment_id,
            "course_id": course_id,
            "module_id": module_id,
            "submission_id": submission_id,
            "score": score,
            "passed": passed,
            "marks_obtained": marks_obtained,
            "total_questions": total_questions,
            "time_seconds": time_seconds,
            "submitted_at": submitted_at.isoformat() if submitted_at else None,
        },
    )
