from typing import List

from database import get_db
from fastapi import APIRouter, Depends, HTTPException, status
from schemas import LeaderboardItem, ProgressDashboardResponse
from services import get_course_leaderboard, get_student_course_progress
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
