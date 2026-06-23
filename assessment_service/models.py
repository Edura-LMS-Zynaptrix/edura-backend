"""
assessment_service/models.py
SQLAlchemy 2.0 ORM models for the Assessment Service.
Entities: Assessment, Question, Submission, ViolationLog

Matches ER diagram:
- quiz entity: ID, type, student_type, password (quiz_password for access control)
- exam entity: timed assessment
- quiz_habits / participate: tracked via Submission and ViolationLog
"""
import enum

from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database import Base  # shared Base from database.py


# ---------------------------------------------------------------------------
# ENUMs
# ---------------------------------------------------------------------------

class AssessmentType(str, enum.Enum):
    quiz = "quiz"       # Short practice quiz (matches ER diagram: quiz entity)
    exam = "exam"       # Full timed exam  (matches ER diagram: exam entity)


class QuestionType(str, enum.Enum):
    mcq = "mcq"                     # Multiple-choice question
    true_false = "true_false"
    short_answer = "short_answer"


class SubmissionStatus(str, enum.Enum):
    in_progress = "in_progress"
    submitted = "submitted"
    graded = "graded"


class ViolationType(str, enum.Enum):
    tab_switch = "tab_switch"
    copy_paste = "copy_paste"
    multiple_faces = "multiple_faces"
    no_face = "no_face"
    other = "other"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Assessment(Base):
    """
    Represents a quiz or exam for a course.
    Matches ER diagram: quiz entity (ID, type, password) and exam entity.
    quiz_password allows teacher to restrict access (matches ER: password attribute on quiz).
    """
    __tablename__ = "assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    lesson_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    assessment_type: Mapped[AssessmentType] = mapped_column(
        Enum(AssessmentType, name="assessmenttype"), nullable=False, default=AssessmentType.quiz
    )
    quiz_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    time_limit_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_score: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    pass_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_assessments_course_id", "course_id"),
    )


class Question(Base):
    """
    A single question within an assessment.
    options_json stores MCQ choices as a JSON string (e.g. '["A","B","C","D"]').
    correct_answer stores the correct answer text or index.
    """
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assessment_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[QuestionType] = mapped_column(
        Enum(QuestionType, name="questiontype"), nullable=False, default=QuestionType.mcq
    )
    options_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    correct_answer: Mapped[str] = mapped_column(Text, nullable=False)
    marks: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_questions_assessment_id", "assessment_id"),
    )


class Submission(Base):
    """
    A student's attempt on an assessment.
    Matches ER diagram: participate / quiz_habits entity — tracks student engagement.
    answers_json stores student answers as a JSON string keyed by question_id.
    """
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assessment_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(SubmissionStatus, name="submissionstatus"),
        nullable=False,
        default=SubmissionStatus.in_progress,
    )
    answers_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON object
    score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    submitted_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_submissions_assessment_id", "assessment_id"),
        Index("ix_submissions_student_id", "student_id"),
        Index("ix_submissions_status", "status"),
    )


class ViolationLog(Base):
    """
    Anti-cheating event log for a student's exam submission.
    Records tab switching, copy-paste, camera detection events.
    """
    __tablename__ = "violation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    violation_type: Mapped[ViolationType] = mapped_column(
        Enum(ViolationType, name="violationtype"), nullable=False
    )
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_violation_logs_submission_id", "submission_id"),
        Index("ix_violation_logs_student_id", "student_id"),
    )