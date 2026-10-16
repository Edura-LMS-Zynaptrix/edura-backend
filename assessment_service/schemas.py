from typing import List, Optional

from pydantic import BaseModel


class QuestionPublic(BaseModel):
    id: int
    question_text: str
    question_type: str
    options_json: Optional[str] = None
    marks: int
    position: int

    model_config = {"from_attributes": True}


class AssessmentBase(BaseModel):
    title: str
    description: Optional[str] = None
    assessment_type: str = "quiz"
    time_limit_minutes: Optional[int] = None
    max_score: int = 100
    pass_score: int = 50
    is_published: bool = False


class AssessmentResponse(AssessmentBase):
    id: int
    course_id: int
    lesson_id: Optional[int] = None

    model_config = {"from_attributes": True}


class StartSessionResponse(BaseModel):
    session_id: str
    assessment_id: int
    title: str
    questions: List[QuestionPublic]
    time_remaining_seconds: int


class AnswerItem(BaseModel):
    question_id: int
    selected_option: str  # or selected choice text/letter


class SubmitAssessmentRequest(BaseModel):
    session_id: str
    answers: List[AnswerItem] = []


class SubmitAssessmentResponse(BaseModel):
    session_id: str
    score: float
    passed: bool
    correct_count: int
    total_questions: int
    auto_submitted: bool = False
    requires_manual_review: bool = False


class ViolationRequest(BaseModel):
    session_id: str
    violation_type: str
    detail: Optional[str] = None
    timestamp: Optional[str] = None


class QuestionCreate(BaseModel):
    question_text: str
    question_type: str = "mcq"
    options_json: Optional[str] = None
    correct_answer: str
    marks: int = 1
    position: int = 0


class AssessmentCreate(AssessmentBase):
    course_id: int
    lesson_id: Optional[int] = None
    quiz_password: Optional[str] = None
    questions: Optional[List[QuestionCreate]] = []


class ViolationRecord(BaseModel):
    id: int
    submission_id: int
    student_id: int
    violation_type: str
    detail: Optional[str] = None
    occurred_at: Optional[str] = None

    model_config = {"from_attributes": True}
