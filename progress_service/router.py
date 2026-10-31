from typing import List

from database import get_db
from fastapi import APIRouter, Depends, HTTPException, status
from models import LessonProgress
from schemas import (
    LeaderboardItem,
    LessonProgressItem,
    LessonProgressUpdateRequest,
    ProgressDashboardResponse,
)
from services import (
    get_course_leaderboard,
    get_student_course_progress,
    process_lesson_watched,
)
from sqlalchemy.orm import Session

from shared.auth import require_role

router = APIRouter()

_ALL_ROLES = ["student", "teacher", "admin"]


@router.get(
    "/api/progress/courses/{course_id}",
    response_model=ProgressDashboardResponse,
    tags=["Progress"],
)
@router.get(
    "/courses/{course_id}",
    response_model=ProgressDashboardResponse,
    include_in_schema=False,
)
def get_progress_dashboard(
    course_id: int,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(require_role(_ALL_ROLES)),
):
    """
    AC3: Progress Dashboard Endpoint
    Returns completion percentage, watched lessons count, total lessons, assessment scores, and certificate status.
    """
    student_id = int(user_payload.get("sub", 0))
    if not student_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid student user ID in token",
        )

    res = get_student_course_progress(db, student_id=student_id, course_id=course_id)
    return res


@router.post(
    "/api/progress/courses/{course_id}/lessons/progress",
    response_model=LessonProgressItem,
    tags=["Progress"],
)
@router.post(
    "/courses/{course_id}/lessons/progress",
    response_model=LessonProgressItem,
    include_in_schema=False,
)
def mark_lesson_watched(
    course_id: int,
    body: LessonProgressUpdateRequest,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(require_role(["student"])),
):
    """
    Marks a lesson as watched for the current student, recalculates the
    course's completion percentage, updates their leaderboard points, and
    issues a certificate if this completes the course (all of that logic
    already existed in services.process_lesson_watched — it just had no
    HTTP endpoint calling it, so lesson-watch progress was never persisted).
    """
    student_id = int(user_payload["sub"])
    lp = process_lesson_watched(
        db,
        student_id=student_id,
        course_id=course_id,
        lesson_id=body.lesson_id,
        total_lessons=body.total_lessons,
        watch_duration=body.watch_duration_seconds,
        last_position=body.last_position_seconds,
    )
    return LessonProgressItem(
        lesson_id=lp.lesson_id,
        is_completed=lp.is_completed,
        watch_duration_seconds=lp.watch_duration_seconds,
        last_position_seconds=lp.last_position_seconds,
    )


@router.get(
    "/api/progress/courses/{course_id}/lessons/progress",
    response_model=List[LessonProgressItem],
    tags=["Progress"],
)
@router.get(
    "/courses/{course_id}/lessons/progress",
    response_model=List[LessonProgressItem],
    include_in_schema=False,
)
def get_lesson_progress(
    course_id: int,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(require_role(_ALL_ROLES)),
):
    """Per-lesson completion state for the current student in this course —
    lets the video player show which lessons are already done on load."""
    student_id = int(user_payload["sub"])
    rows = (
        db.query(LessonProgress)
        .filter(
            LessonProgress.student_id == student_id,
            LessonProgress.course_id == course_id,
        )
        .all()
    )
    return [
        LessonProgressItem(
            lesson_id=r.lesson_id,
            is_completed=r.is_completed,
            watch_duration_seconds=r.watch_duration_seconds,
            last_position_seconds=r.last_position_seconds,
        )
        for r in rows
    ]


@router.get(
    "/api/progress/courses/{course_id}/leaderboard",
    response_model=List[LeaderboardItem],
    tags=["Leaderboard"],
)
@router.get(
    "/courses/{course_id}/leaderboard",
    response_model=List[LeaderboardItem],
    include_in_schema=False,
)
def get_leaderboard(
    course_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(_ALL_ROLES)),
):
    """
    AC4: Leaderboard Endpoint
    Returns top 10 students sorted by total points descending, served from Redis cache.
    """
    items = get_course_leaderboard(db, course_id=course_id, limit=10)
    return items
