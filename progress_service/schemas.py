from typing import List, Optional

from pydantic import BaseModel


class LessonProgressUpdateRequest(BaseModel):
    lesson_id: int
    total_lessons: Optional[int] = None
    watch_duration_seconds: int = 0
    last_position_seconds: int = 0


class LessonProgressItem(BaseModel):
    lesson_id: int
    is_completed: bool
    watch_duration_seconds: int
    last_position_seconds: int


class AssessmentScoreItem(BaseModel):
    assessment_id: int
    score: float
    passed: bool


class ProgressDashboardResponse(BaseModel):
    completion_percentage: float
    watched_lessons: int
    total_lessons: int
    assessment_scores: List[AssessmentScoreItem]
    certificate_issued: bool


class LeaderboardItem(BaseModel):
    rank: int
    student_id: int
    points: int
