import json
import random
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from database import get_db
from events import publish_assessment_graded
from fastapi import APIRouter, Depends, HTTPException, Response, status
from models import (
    Assessment,
    Question,
    QuestionType,
    Submission,
    SubmissionStatus,
    ViolationLog,
    ViolationType,
)
from redis_client import get_redis_client
from schemas import (
    AssessmentCreate,
    AssessmentResponse,
    QuestionPublic,
    StartSessionResponse,
    SubmitAssessmentRequest,
    SubmitAssessmentResponse,
    ViolationRecord,
    ViolationRequest,
)
from sqlalchemy.orm import Session

from shared.auth import require_role

router = APIRouter()

_ALL_ROLES = ["student", "teacher", "admin"]


def _get_assessment_or_404(assessment_id: int, db: Session) -> Assessment:
    assessment = db.query(Assessment).filter(Assessment.id == assessment_id).first()
    if not assessment:
        raise HTTPException(
            status_code=404,
            detail={"error": "ASSESSMENT_NOT_FOUND", "message": "Assessment not found"},
        )
    return assessment


# ---------------------------------------------------------------------------
# Assessment endpoints
# ---------------------------------------------------------------------------


@router.get("/health", tags=["Health"])
@router.get("/api/assessments/health", tags=["Health"])
async def assessment_health():
    return {"status": "ok", "service": "assessment_service"}


@router.get("/{assessment_id}", response_model=AssessmentResponse)
@router.get("/api/assessments/{assessment_id}", response_model=AssessmentResponse)
async def get_assessment(
    assessment_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(_ALL_ROLES)),
):
    """
    Get assessment metadata by ID.
    """
    return _get_assessment_or_404(assessment_id, db)


@router.post("/{assessment_id}/start", response_model=StartSessionResponse)
@router.post(
    "/api/assessments/{assessment_id}/start", response_model=StartSessionResponse
)
async def start_assessment_session(
    assessment_id: int,
    db: Session = Depends(get_db),
    payload: dict = Depends(require_role(_ALL_ROLES)),
):
    """
    Start a timed assessment session (AC1 & AC5).
    """
    student_id = int(payload["sub"])
    redis_c = get_redis_client()

    # Check duplicate active session (AC5)
    active_key = f"session:active:{student_id}:{assessment_id}"
    existing_session_id = redis_c.get(active_key)
    if existing_session_id:
        session_key = f"session:assessment:{existing_session_id}"
        remaining = redis_c.ttl(session_key)
        if remaining > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "SESSION_ALREADY_ACTIVE",
                    "session_id": existing_session_id,
                    "time_remaining_seconds": remaining,
                },
            )

    assessment = _get_assessment_or_404(assessment_id, db)

    # Fetch questions
    questions = (
        db.query(Question)
        .filter(Question.assessment_id == assessment_id)
        .order_by(Question.position.asc())
        .all()
    )

    # Create submission record
    submission = Submission(
        assessment_id=assessment_id,
        student_id=student_id,
        status=SubmissionStatus.in_progress,
        started_at=datetime.now(timezone.utc),
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)

    # Generate session ID
    session_id = f"sess_{uuid.uuid4().hex}"
    time_limit_seconds = (
        (assessment.time_limit_minutes * 60) if assessment.time_limit_minutes else 3600
    )

    # Save session data to Redis
    session_payload = {
        "session_id": session_id,
        "submission_id": submission.id,
        "student_id": student_id,
        "assessment_id": assessment_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "time_limit_seconds": time_limit_seconds,
    }
    redis_c.set(
        f"session:assessment:{session_id}",
        json.dumps(session_payload),
        ex=time_limit_seconds,
    )
    redis_c.set(active_key, session_id, ex=time_limit_seconds)

    # Obfuscate questions (remove correct_answer) and shuffle
    shuffled_questions = list(questions)
    random.shuffle(shuffled_questions)

    public_questions = [
        QuestionPublic(
            id=q.id,
            question_text=q.question_text,
            question_type=(
                q.question_type.value
                if hasattr(q.question_type, "value")
                else str(q.question_type)
            ),
            options_json=q.options_json,
            marks=q.marks,
            position=q.position,
        )
        for q in shuffled_questions
    ]

    return StartSessionResponse(
        session_id=session_id,
        assessment_id=assessment_id,
        title=assessment.title,
        questions=public_questions,
        time_remaining_seconds=time_limit_seconds,
    )


@router.post("/{assessment_id}/submit", response_model=SubmitAssessmentResponse)
@router.post(
    "/api/assessments/{assessment_id}/submit", response_model=SubmitAssessmentResponse
)
async def submit_assessment(
    assessment_id: int,
    body: SubmitAssessmentRequest,
    db: Session = Depends(get_db),
    payload: dict = Depends(require_role(_ALL_ROLES)),
):
    """
    Submit assessment for auto-grading or process auto-submit on expiry (AC2 & AC3).
    """
    student_id = int(payload["sub"])
    redis_c = get_redis_client()

    session_key = f"session:assessment:{body.session_id}"
    active_key = f"session:active:{student_id}:{assessment_id}"
    raw_session_data = redis_c.get(session_key)

    auto_submitted = False
    submission_id = None

    if raw_session_data:
        session_data = json.loads(raw_session_data)
        submission_id = session_data.get("submission_id")
    else:
        # Session expired or invalid -> AC3 Auto-submit
        auto_submitted = True

    assessment = _get_assessment_or_404(assessment_id, db)
    questions = db.query(Question).filter(Question.assessment_id == assessment_id).all()

    answers_dict = {a.question_id: a.selected_option for a in body.answers}

    total_possible_marks = 0
    obtained_marks = 0
    correct_count = 0
    total_questions = len(questions)

    for q in questions:
        total_possible_marks += q.marks
        user_answer = answers_dict.get(q.id, "").strip()

        if q.question_type in (QuestionType.mcq, QuestionType.true_false):
            if (
                user_answer
                and user_answer.lower() == str(q.correct_answer).strip().lower()
            ):
                obtained_marks += q.marks
                correct_count += 1
        elif q.question_type == QuestionType.short_answer:
            # Short answer requires manual review per BR-002; 0 marks auto-graded
            pass

    requires_manual_review = any(
        q.question_type == QuestionType.short_answer for q in questions
    )

    score = (
        round((obtained_marks / total_possible_marks) * 100, 2)
        if total_possible_marks > 0
        else 0.0
    )
    passed = score >= assessment.pass_score

    # Update or create Submission DB record
    submission = None
    if submission_id:
        submission = db.query(Submission).filter(Submission.id == submission_id).first()

    if not submission:
        submission = (
            db.query(Submission)
            .filter(
                Submission.assessment_id == assessment_id,
                Submission.student_id == student_id,
                Submission.status == SubmissionStatus.in_progress,
            )
            .first()
        )

    if not submission:
        submission = Submission(
            assessment_id=assessment_id,
            student_id=student_id,
            started_at=datetime.now(timezone.utc),
        )
        db.add(submission)

    submission.status = SubmissionStatus.graded
    submission.score = score
    submission.requires_manual_review = requires_manual_review
    submission.answers_json = json.dumps([a.model_dump() for a in body.answers])
    submission.submitted_at = datetime.now(timezone.utc)
    db.commit()

    # Clear Redis keys
    redis_c.delete(session_key)
    redis_c.delete(active_key)

    # Publish RabbitMQ event
    publish_assessment_graded(
        student_id=student_id,
        assessment_id=assessment_id,
        score=score,
        passed=passed,
        course_id=assessment.course_id,
    )

    return SubmitAssessmentResponse(
        session_id=body.session_id,
        score=score,
        passed=passed,
        correct_count=correct_count,
        total_questions=total_questions,
        auto_submitted=auto_submitted,
        requires_manual_review=requires_manual_review,
    )


@router.post("/{assessment_id}/violations", status_code=status.HTTP_204_NO_CONTENT)
@router.post(
    "/api/assessments/{assessment_id}/violations",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def record_violation(
    assessment_id: int,
    body: ViolationRequest,
    db: Session = Depends(get_db),
    payload: dict = Depends(require_role(_ALL_ROLES)),
):
    """
    Record anti-cheat tab-switch violation (AC4).
    """
    student_id = int(payload["sub"])
    redis_c = get_redis_client()

    session_key = f"session:assessment:{body.session_id}"
    raw_session_data = redis_c.get(session_key)

    submission_id = None
    if raw_session_data:
        session_data = json.loads(raw_session_data)
        submission_id = session_data.get("submission_id")

    if not submission_id:
        sub_rec = (
            db.query(Submission)
            .filter(
                Submission.assessment_id == assessment_id,
                Submission.student_id == student_id,
            )
            .order_by(Submission.id.desc())
            .first()
        )
        if sub_rec:
            submission_id = sub_rec.id

    if not submission_id:
        # Create an in-progress submission fallback if none exists
        new_sub = Submission(
            assessment_id=assessment_id,
            student_id=student_id,
            status=SubmissionStatus.in_progress,
            started_at=datetime.now(timezone.utc),
        )
        db.add(new_sub)
        db.commit()
        db.refresh(new_sub)
        submission_id = new_sub.id

    # Parse violation_type
    v_type_str = body.violation_type.lower()
    if v_type_str in ("tab_switch", "tab-switch"):
        v_enum = ViolationType.tab_switch
    elif v_type_str in ("copy_paste", "copy-paste"):
        v_enum = ViolationType.copy_paste
    elif v_type_str in ("multiple_faces", "multiple-faces"):
        v_enum = ViolationType.multiple_faces
    elif v_type_str in ("no_face", "no-face"):
        v_enum = ViolationType.no_face
    else:
        v_enum = ViolationType.other

    violation_log = ViolationLog(
        submission_id=submission_id,
        student_id=student_id,
        violation_type=v_enum,
        detail=body.detail or f"Violation recorded: {body.violation_type}",
        occurred_at=datetime.now(timezone.utc),
    )
    db.add(violation_log)
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/", response_model=AssessmentResponse, status_code=status.HTTP_201_CREATED)
@router.post(
    "/api/assessments",
    response_model=AssessmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_assessment(
    body: AssessmentCreate,
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(["teacher", "admin"])),
):
    """
    Create an assessment with questions (for teachers and admins).
    """
    a_type = (
        AssessmentType.exam
        if str(body.assessment_type).lower() == "exam"
        else AssessmentType.quiz
    )
    assessment = Assessment(
        course_id=body.course_id,
        lesson_id=body.lesson_id,
        title=body.title,
        description=body.description,
        assessment_type=a_type,
        quiz_password=body.quiz_password,
        time_limit_minutes=body.time_limit_minutes,
        max_score=body.max_score,
        pass_score=body.pass_score,
        is_published=body.is_published,
    )
    db.add(assessment)
    db.commit()
    db.refresh(assessment)

    if body.questions:
        for idx, q_data in enumerate(body.questions, start=1):
            q_type = QuestionType.mcq
            if q_data.question_type == "true_false":
                q_type = QuestionType.true_false
            elif q_data.question_type == "short_answer":
                q_type = QuestionType.short_answer

            question = Question(
                assessment_id=assessment.id,
                question_text=q_data.question_text,
                question_type=q_type,
                options_json=q_data.options_json,
                correct_answer=q_data.correct_answer,
                marks=q_data.marks,
                position=q_data.position or idx,
            )
            db.add(question)
        db.commit()

    return assessment


@router.delete("/{assessment_id}", status_code=status.HTTP_200_OK)
@router.delete("/api/assessments/{assessment_id}", status_code=status.HTTP_200_OK)
async def delete_assessment(
    assessment_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(["teacher", "admin"])),
):
    """
    Delete assessment and its associated questions, submissions, and violations.
    """
    subs = db.query(Submission).filter(Submission.assessment_id == assessment_id).all()
    sub_ids = [s.id for s in subs]
    if sub_ids:
        db.query(ViolationLog).filter(
            ViolationLog.submission_id.in_(sub_ids)
        ).delete(synchronize_session=False)
        db.query(Submission).filter(
            Submission.id.in_(sub_ids)
        ).delete(synchronize_session=False)

    db.query(Question).filter(
        Question.assessment_id == assessment_id
    ).delete(synchronize_session=False)
    db.query(Assessment).filter(
        Assessment.id == assessment_id
    ).delete(synchronize_session=False)
    db.commit()
    return {"message": "Assessment and related records deleted successfully"}


@router.get("/violations", response_model=List[ViolationRecord])
@router.get("/api/assessments/violations", response_model=List[ViolationRecord])
@router.get("/api/admin/violations", response_model=List[ViolationRecord])
async def list_violations(
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(["admin"])),
):
    """
    List all violation logs for administration.
    """
    records = db.query(ViolationLog).order_by(ViolationLog.id.desc()).all()
    return [
        ViolationRecord(
            id=v.id,
            submission_id=v.submission_id,
            student_id=v.student_id,
            violation_type=(
                v.violation_type.value
                if hasattr(v.violation_type, "value")
                else str(v.violation_type)
            ),
            detail=v.detail,
            occurred_at=v.occurred_at.isoformat() if v.occurred_at else None,
        )
        for v in records
    ]

