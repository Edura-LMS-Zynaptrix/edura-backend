from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class QuestionPublic(BaseModel):
    id: int
    question_text: str
    question_type: str
    options_json: Optional[str] = None
    question_image_url: Optional[str] = None
    # Aligned with the options in options_json; null where an option has no image.
    option_image_urls: List[Optional[str]] = []
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
    image_url: Optional[str] = None
    image_public_id: Optional[str] = None
    position: int = 0
    scheduled_start: Optional[datetime] = None


class AssessmentResponse(AssessmentBase):
    id: int
    course_id: int
    module_id: Optional[int] = None
    lesson_id: Optional[int] = None
    question_count: int = 0

    model_config = {"from_attributes": True}


class AssessmentCreate(AssessmentBase):
    course_id: int
    module_id: Optional[int] = None
    lesson_id: Optional[int] = None


class AssessmentUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    module_id: Optional[int] = None
    lesson_id: Optional[int] = None
    assessment_type: Optional[str] = None
    time_limit_minutes: Optional[int] = None
    max_score: Optional[int] = None
    pass_score: Optional[int] = None
    is_published: Optional[bool] = None
    image_url: Optional[str] = None
    image_public_id: Optional[str] = None
    position: Optional[int] = None
    scheduled_start: Optional[datetime] = None


class QuestionOptionInput(BaseModel):
    option_text: str
    is_correct: bool = False
    option_image_url: Optional[str] = None
    option_image_public_id: Optional[str] = None


class QuestionCreate(BaseModel):
    question_text: str
    question_type: str = "mcq"
    options: List[QuestionOptionInput] = []
    correct_answer: Optional[str] = None
    explanation: Optional[str] = None
    question_image_url: Optional[str] = None
    question_image_public_id: Optional[str] = None
    marks: int = 1
    position: int = 0


class QuestionUpdate(BaseModel):
    question_text: Optional[str] = None
    question_type: Optional[str] = None
    options: Optional[List[QuestionOptionInput]] = None
    correct_answer: Optional[str] = None
    explanation: Optional[str] = None
    question_image_url: Optional[str] = None
    question_image_public_id: Optional[str] = None
    marks: Optional[int] = None
    position: Optional[int] = None


class OptionImage(BaseModel):
    url: str
    public_id: Optional[str] = None


class QuestionAdminResponse(BaseModel):
    """Full question detail for the owning teacher/admin — includes the
    correct answer, unlike QuestionPublic served to students taking the exam."""

    id: int
    assessment_id: int
    question_text: str
    question_type: str
    options: List[str] = []
    option_images: List[Optional[OptionImage]] = []
    correct_answer: str
    explanation: Optional[str] = None
    question_image_url: Optional[str] = None
    question_image_public_id: Optional[str] = None
    marks: int
    position: int

    model_config = {"from_attributes": True}


class CsvImportResult(BaseModel):
    message: str
    questions_created: int
    errors: List[str] = []


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


class ReviewOption(BaseModel):
    id: str  # options are stored as text, so the text doubles as the id
    option_text: str
    option_image_url: Optional[str] = None
    order_number: int


class ReviewItem(BaseModel):
    question_id: int
    question_text: str
    question_image_url: Optional[str] = None
    explanation: Optional[str] = None
    selected_option_id: Optional[str] = None
    correct_option_id: str
    is_correct: bool
    options: List[ReviewOption] = []


class SubmissionRanking(BaseModel):
    exam_id: int
    course_id: int
    exam_title: str
    overall_rank: Optional[int] = None


class BadgeResponse(BaseModel):
    id: int
    name: str
    description: str
    criteria_key: str
    category: str
    icon_name: str

    model_config = {"from_attributes": True}


class UserBadgeResponse(BaseModel):
    id: int
    badge_id: int
    user_id: int
    awarded_at: datetime
    badge: BadgeResponse


class BadgeDashboardResponse(BaseModel):
    available_badges: List[BadgeResponse]
    earned_badges: List[UserBadgeResponse]


class SubmitAssessmentResponse(BaseModel):
    session_id: str
    score: float
    passed: bool
    correct_count: int
    total_questions: int
    auto_submitted: bool = False
    time_taken_seconds: int = 0
    review: List[ReviewItem] = []
    ranking: Optional[SubmissionRanking] = None
    new_badges: List[BadgeResponse] = []


class MyAttemptSummary(BaseModel):
    assessment_id: int
    attempt_count: int
    last_score: float
    passed: bool  # any graded attempt reached pass_score


class ViolationRequest(BaseModel):
    session_id: str
    violation_type: str
    detail: Optional[str] = None
    timestamp: Optional[str] = None


class MyAttemptItem(BaseModel):
    attempt_id: int
    exam_id: int
    attempt_number: int
    course_id: int
    sub_course_id: Optional[int] = None
    exam_title: str
    course_title: str
    subject: str = ""
    marks_obtained: int
    total_questions: int
    time_taken_seconds: int
    status: str
    started_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    overall_rank: Optional[int] = None
    district_rank: Optional[int] = None


class AttemptRanking(BaseModel):
    exam_id: int
    course_id: int
    exam_title: str
    course_title: str = ""
    subject: str = ""
    overall_rank: Optional[int] = None
    district_rank: Optional[int] = None


class AttemptReviewResponse(BaseModel):
    attempt_id: int
    student_id: int
    score: float
    passed: bool
    marks_obtained: int
    total_questions: int
    time_taken_seconds: int
    submitted_at: Optional[datetime] = None
    review: List[ReviewItem] = []
    ranking: AttemptRanking

