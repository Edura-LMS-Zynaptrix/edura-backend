from typing import List

from pydantic import BaseModel


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
