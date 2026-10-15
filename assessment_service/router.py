import csv
import io
import json
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

from badges import evaluate_after_attempt
from bulk_import import (
    apply_question_images,
    bulk_import,
    import_question_rows,
    safe_zip,
    template_zip,
)
from course_client import get_course_instructor_id, get_course_titles
from database import get_db
from enrollment_client import is_enrolled
from events import publish_assessment_graded
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from models import (
    Assessment,
    AssessmentType,
    Badge,
    Question,
    QuestionType,
    Submission,
    SubmissionStatus,
    UserBadge,
    ViolationLog,
    ViolationType,
)
from rankings import rank_rows
from redis_client import get_redis_client
from schemas import (
    AssessmentCreate,
    AssessmentResponse,
    AssessmentUpdate,
    AttemptRanking,
    AttemptReviewResponse,
    BadgeDashboardResponse,
    BadgeResponse,
    CsvImportResult,
    MyAttemptItem,
    MyAttemptSummary,
    QuestionAdminResponse,
    QuestionCreate,
    QuestionPublic,
    QuestionUpdate,
    ReviewItem,
    ReviewOption,
    StartSessionResponse,
    SubmissionRanking,
    SubmitAssessmentRequest,
    SubmitAssessmentResponse,
    UserBadgeResponse,
    ViolationRequest,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from user_client import get_profiles

from shared.auth import get_optional_payload, require_role

logger = logging.getLogger(__name__)

router = APIRouter()

_ALL_ROLES = ["student", "teacher", "admin"]
_TEACHER_ADMIN = ["teacher", "admin"]


def _get_assessment_or_404(assessment_id: int, db: Session) -> Assessment:
    assessment = db.query(Assessment).filter(Assessment.id == assessment_id).first()
    if not assessment:
        raise HTTPException(
            status_code=404,
            detail={"error": "ASSESSMENT_NOT_FOUND", "message": "Assessment not found"},
        )
    return assessment


def _build_review(questions: list[Question], answers_dict: dict) -> list[ReviewItem]:
    """Per-question breakdown for a graded submission — options are stored as
    plain option text (not separate rows with ids), so the option text itself
    doubles as its "id", matching how /start already exposes options to the UI."""
    review = []
    for q in questions:
        user_answer = (answers_dict.get(q.id) or "").strip()
        is_correct = (
            q.question_type in (QuestionType.mcq, QuestionType.true_false)
            and bool(user_answer)
            and user_answer.lower() == str(q.correct_answer).strip().lower()
        )
        try:
            option_texts = json.loads(q.options_json) if q.options_json else []
        except (ValueError, TypeError):
            option_texts = []
        images = _option_images(q, len(option_texts))
        review.append(
            ReviewItem(
                question_id=q.id,
                question_text=q.question_text,
                question_image_url=q.question_image_url,
                explanation=q.explanation,
                selected_option_id=user_answer or None,
                correct_option_id=str(q.correct_answer),
                is_correct=is_correct,
                options=[
                    ReviewOption(
                        id=text,
                        option_text=text,
                        option_image_url=img["url"] if img else None,
                        order_number=idx,
                    )
                    for idx, (text, img) in enumerate(
                        zip(option_texts, images, strict=False)
                    )
                ],
            )
        )
    return review


def _answers_of(submission: Submission) -> dict[int, str]:
    answers: dict[int, str] = {}
    if submission.answers_json:
        try:
            for a in json.loads(submission.answers_json):
                answers[int(a["question_id"])] = a.get("selected_option") or ""
        except (ValueError, TypeError, KeyError):
            pass
    return answers


def _correct_count(questions: list[Question], answers: dict[int, str]) -> int:
    return sum(
        1
        for q in questions
        if q.question_type in (QuestionType.mcq, QuestionType.true_false)
        and (answers.get(q.id) or "").strip().lower()
        == str(q.correct_answer).strip().lower()
    )


def _time_taken(submission: Submission) -> int:
    if submission.started_at and submission.submitted_at:
        return max(
            0, int((submission.submitted_at - submission.started_at).total_seconds())
        )
    return 0


async def _my_ranks(
    assessment_id: int, student_id: int, db: Session
) -> tuple[int | None, int | None]:
    """(island, district) place on the exam's leaderboard — the same first-attempt ranking /rankings shows."""
    mine = next(
        (
            r
            for r in await _exam_board(assessment_id, db)
            if r["student_id"] == student_id
        ),
        None,
    )
    return (mine["rank"], mine["district_rank"]) if mine else (None, None)


def _get_question_or_404(question_id: int, db: Session) -> Question:
    question = db.query(Question).filter(Question.id == question_id).first()
    if not question:
        raise HTTPException(
            status_code=404,
            detail={"error": "QUESTION_NOT_FOUND", "message": "Question not found"},
        )
    return question


async def _assert_course_owner(
    course_id: int, requester_id: int, requester_role: str
) -> None:
    if requester_role == "admin":
        return
    instructor_id = await get_course_instructor_id(course_id)
    if instructor_id is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "COURSE_NOT_FOUND", "message": "Parent course not found"},
        )
    if instructor_id != requester_id:
        raise HTTPException(status_code=403, detail={"error": "NOT_COURSE_OWNER"})


def _is_public_viewer(payload: dict | None) -> bool:
    return not payload or payload.get("role", "").lower() == "student"


def _question_counts(assessment_ids: list[int], db: Session) -> dict[int, int]:
    if not assessment_ids:
        return {}
    return dict(
        db.query(Question.assessment_id, func.count(Question.id))
        .filter(Question.assessment_id.in_(assessment_ids))
        .group_by(Question.assessment_id)
        .all()
    )


def _to_response(
    a: Assessment, question_count: int | None = None, db: Session | None = None
) -> AssessmentResponse:
    if question_count is None and db is not None:
        question_count = _question_counts([a.id], db).get(a.id, 0)
    return AssessmentResponse(
        id=a.id,
        course_id=a.course_id,
        module_id=a.module_id,
        lesson_id=a.lesson_id,
        title=a.title,
        description=a.description,
        assessment_type=(
            a.assessment_type.value
            if hasattr(a.assessment_type, "value")
            else a.assessment_type
        ),
        time_limit_minutes=a.time_limit_minutes,
        max_score=a.max_score,
        pass_score=a.pass_score,
        is_published=a.is_published,
        image_url=a.image_url,
        image_public_id=a.image_public_id,
        position=a.position or 0,
        scheduled_start=a.scheduled_start,
        question_count=question_count or 0,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Naive datetimes from clients are taken as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _option_images_json(options) -> str | None:
    """Serialise option images aligned with the option list; None when no option has one."""
    images = [
        (
            {"url": o.option_image_url, "public_id": o.option_image_public_id}
            if o.option_image_url
            else None
        )
        for o in options
    ]
    return json.dumps(images) if any(images) else None


def _option_images(q: Question, option_count: int) -> list[dict | None]:
    try:
        images = json.loads(q.option_images_json) if q.option_images_json else []
    except (ValueError, TypeError):
        images = []
    # Pad/trim so callers can always zip it with the options.
    return (images + [None] * option_count)[:option_count]


def _question_to_admin_response(q: Question) -> QuestionAdminResponse:
    try:
        options = json.loads(q.options_json) if q.options_json else []
    except (ValueError, TypeError):
        options = []
    return QuestionAdminResponse(
        id=q.id,
        assessment_id=q.assessment_id,
        question_text=q.question_text,
        question_type=(
            q.question_type.value
            if hasattr(q.question_type, "value")
            else str(q.question_type)
        ),
        options=options,
        option_images=_option_images(q, len(options)),
        correct_answer=q.correct_answer,
        explanation=q.explanation,
        question_image_url=q.question_image_url,
        question_image_public_id=q.question_image_public_id,
        marks=q.marks,
        position=q.position,
    )


# ---------------------------------------------------------------------------
# Assessment management endpoints (teacher/admin authoring)
# ---------------------------------------------------------------------------


@router.get("/", response_model=list[AssessmentResponse])
async def list_assessments(
    course_id: int | None = Query(None),
    module_id: int | None = Query(
        None, description="course_service module (lesson) id"
    ),
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    """List assessments of a course and/or one of its lessons. Anonymous visitors and students only see published ones."""
    if course_id is None and module_id is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "FILTER_REQUIRED",
                "message": "Provide course_id or module_id",
            },
        )
    q = db.query(Assessment)
    if course_id is not None:
        q = q.filter(Assessment.course_id == course_id)
    if module_id is not None:
        q = q.filter(Assessment.module_id == module_id)
    if _is_public_viewer(payload):
        q = q.filter(Assessment.is_published.is_(True))
    assessments = q.order_by(Assessment.position, Assessment.id).all()
    counts = _question_counts([a.id for a in assessments], db)
    return [_to_response(a, counts.get(a.id, 0)) for a in assessments]


def _int_list(raw: str | None) -> list[int]:
    try:
        return [int(x) for x in (raw or "").split(",") if x.strip()][:500]
    except ValueError:
        raise HTTPException(status_code=422, detail={"error": "INVALID_IDS"})


@router.get("/internal/students/{student_id}/exam-progress")
def student_exam_progress_internal(
    student_id: int,
    course_ids: str | None = Query(
        None, description="Courses the student can open in full"
    ),
    lesson_ids: str | None = Query(
        None, description="Single lessons granted on their own"
    ),
    db: Session = Depends(get_db),
):
    """Service-to-service only: every published exam the student can take, with their graded attempts."""
    courses, lessons = _int_list(course_ids), _int_list(lesson_ids)
    if not courses and not lessons:
        return []
    q = db.query(Assessment).filter(Assessment.is_published.is_(True))
    if courses and lessons:
        q = q.filter(
            Assessment.course_id.in_(courses) | Assessment.module_id.in_(lessons)
        )
    elif courses:
        q = q.filter(Assessment.course_id.in_(courses))
    else:
        q = q.filter(Assessment.module_id.in_(lessons))
    exams = q.order_by(Assessment.course_id, Assessment.position, Assessment.id).all()
    if not exams:
        return []
    ids = [e.id for e in exams]
    questions_by_exam: dict[int, list[Question]] = {}
    for question in db.query(Question).filter(Question.assessment_id.in_(ids)):
        questions_by_exam.setdefault(question.assessment_id, []).append(question)
    attempts_by_exam: dict[int, list[dict]] = {}
    for sub in (
        db.query(Submission)
        .filter(
            Submission.student_id == student_id,
            Submission.assessment_id.in_(ids),
            Submission.status == SubmissionStatus.graded,
        )
        .order_by(Submission.submitted_at, Submission.id)
    ):
        questions = questions_by_exam.get(sub.assessment_id, [])
        attempts_by_exam.setdefault(sub.assessment_id, []).append(
            {
                "id": sub.id,
                "score_pct": round(float(sub.score or 0)),
                "marks_obtained": _correct_count(questions, _answers_of(sub)),
                "total_questions": len(questions),
                "time_seconds": _time_taken(sub),
                "submitted_at": (
                    sub.submitted_at.isoformat() if sub.submitted_at else None
                ),
            }
        )
    return [
        {
            "id": e.id,
            "title": e.title,
            "course_id": e.course_id,
            "module_id": e.module_id,
            "order_number": e.position or 0,
            "attempts": attempts_by_exam.get(e.id, []),
        }
        for e in exams
    ]


@router.get("/internal/students/{student_id}/badge-count")
def student_badge_count_internal(student_id: int, db: Session = Depends(get_db)):
    return {
        "count": db.query(UserBadge).filter(UserBadge.student_id == student_id).count()
    }


@router.get("/internal/courses/{course_id}/first-attempts")
def course_first_attempts_internal(course_id: int, db: Session = Depends(get_db)):
    """Service-to-service only: each student's FIRST graded attempt per exam (struggle analysis)."""
    exams = db.query(Assessment).filter(Assessment.course_id == course_id).all()
    if not exams:
        return {"exams": [], "attempts": []}
    first: dict[tuple[int, int], Submission] = {}
    for sub in (
        db.query(Submission)
        .filter(
            Submission.assessment_id.in_([e.id for e in exams]),
            Submission.status == SubmissionStatus.graded,
        )
        .order_by(Submission.submitted_at, Submission.id)
    ):
        first.setdefault((sub.student_id, sub.assessment_id), sub)
    return {
        "exams": [
            {"id": e.id, "title": e.title, "module_id": e.module_id} for e in exams
        ],
        "attempts": [
            {"student_id": sid, "exam_id": eid, "score_pct": float(sub.score or 0)}
            for (sid, eid), sub in first.items()
        ],
    }


SL_TIME = timezone(timedelta(hours=5, minutes=30))


@router.get("/internal/admin-stats")
def assessment_admin_stats_internal(db: Session = Depends(get_db)):
    """Service-to-service only (blocked at the gateway): exam figures for the admin dashboard."""
    graded = Submission.status == SubmissionStatus.graded
    months: dict[tuple[int, int], int] = {}
    for (when,) in db.query(Submission.submitted_at).filter(
        graded, Submission.submitted_at.isnot(None)
    ):
        local = (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).astimezone(
            SL_TIME
        )
        months[(local.year, local.month)] = months.get((local.year, local.month), 0) + 1

    top = (
        db.query(
            Submission.assessment_id,
            func.count(Submission.id),
            func.avg(Submission.score),
        )
        .filter(graded)
        .group_by(Submission.assessment_id)
        .order_by(func.count(Submission.id).desc())
        .limit(8)
        .all()
    )
    titles = (
        {
            a.id: a
            for a in db.query(Assessment)
            .filter(Assessment.id.in_([row[0] for row in top]))
            .all()
        }
        if top
        else {}
    )
    recent = (
        db.query(Assessment)
        .order_by(Assessment.created_at.desc(), Assessment.id.desc())
        .limit(5)
        .all()
    )
    return {
        "published_exams": db.query(Assessment)
        .filter(Assessment.is_published.is_(True))
        .count(),
        "graded_attempts": db.query(Submission).filter(graded).count(),
        "attempts_by_month": [
            {"year": y, "month": m, "count": months[(y, m)]}
            for (y, m) in sorted(months)[-12:]
        ],
        "top_exams": [
            {
                "exam_id": exam_id,
                "title": titles[exam_id].title,
                "course_id": titles[exam_id].course_id,
                "attempt_count": count,
                "avg_score": round(float(avg or 0), 1),
            }
            for exam_id, count, avg in top
            if exam_id in titles
        ],
        "recent_exams": [
            {
                "title": a.title,
                "course_id": a.course_id,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in recent
        ],
    }


@router.get("/internal/stats")
async def assessment_stats_internal(db: Session = Depends(get_db)):
    """Service-to-service only (blocked at the gateway): counts for the public homepage."""
    return {
        "published_exams": db.query(Assessment)
        .filter(Assessment.is_published.is_(True))
        .count(),
        "graded_attempts": db.query(Submission)
        .filter(Submission.status == SubmissionStatus.graded)
        .count(),
    }


# ---------------------------------------------------------------------------
# Rankings (public, static paths, so they sit above GET /{assessment_id})
# ---------------------------------------------------------------------------


def _get_published_or_404(assessment_id: int, db: Session) -> Assessment:
    assessment = db.query(Assessment).filter(Assessment.id == assessment_id).first()
    if not assessment or not assessment.is_published:
        raise HTTPException(
            status_code=404,
            detail={"error": "ASSESSMENT_NOT_FOUND", "message": "Assessment not found"},
        )
    return assessment


async def _exam_board(assessment_id: int, db: Session) -> list[dict]:
    """Ranked first attempts for one exam, students only."""
    first: dict[int, Submission] = {}
    for sub in (
        db.query(Submission)
        .filter(
            Submission.assessment_id == assessment_id,
            Submission.status == SubmissionStatus.graded,
        )
        .order_by(Submission.submitted_at, Submission.id)
    ):
        first.setdefault(sub.student_id, sub)
    if not first:
        return []
    profiles = await get_profiles(list(first))
    rows = []
    for student_id, sub in first.items():
        profile = (profiles or {}).get(student_id)
        # Staff test attempts stay off the board; if user_service is down, show everyone unnamed.
        if profiles is not None and (
            not profile or str(profile.get("role", "")).lower() != "student"
        ):
            continue
        profile = profile or {}
        rows.append(
            {
                "student_id": student_id,
                "score": float(sub.score or 0),
                "time_seconds": _time_taken(sub),
                "full_name": profile.get("full_name") or "Student",
                "school": profile.get("school") or "",
                "district": profile.get("district") or "",
                "grade": profile.get("grade") or 0,
            }
        )
    return rank_rows(rows)


@router.get("/rankings/exams")
async def ranking_exams(
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    """Published exams with at least one graded attempt; a signed-in student sees only exams they sat."""
    attempted = select(Submission.assessment_id).where(
        Submission.status == SubmissionStatus.graded
    )
    if payload and str(payload.get("role", "")).lower() == "student":
        attempted = attempted.where(Submission.student_id == int(payload["sub"]))
    exams = (
        db.query(Assessment)
        .filter(
            Assessment.is_published.is_(True), Assessment.id.in_(attempted.distinct())
        )
        .all()
    )
    titles = await get_course_titles([e.course_id for e in exams])
    rows = [
        {
            "exam_id": str(e.id),
            "exam_title": e.title,
            "course_title": titles.get(e.course_id, ""),
            "subject": titles.get(e.course_id, ""),
        }
        for e in exams
    ]
    return sorted(rows, key=lambda r: (r["course_title"], r["exam_title"]))


@router.get("/rankings/leaderboard")
async def ranking_leaderboard(
    exam_id: int,
    district: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    """Public leaderboard of one exam (first attempts). Island and district ranks."""
    _get_published_or_404(exam_id, db)
    board = await _exam_board(exam_id, db)
    if district and district.strip():
        wanted = district.strip().lower()
        board = [r for r in board if wanted in r["district"].lower()]
    me = int(payload["sub"]) if payload else None
    return [
        {
            "rank": r["rank"],
            "district_rank": r["district_rank"],
            "full_name": r["full_name"],
            "school": r["school"],
            "district": r["district"],
            "grade": r["grade"],
            "score": round(r["score"]),
            "time_taken_seconds": r["time_seconds"],
            "attempts": 1,
            "is_current_user": r["student_id"] == me,
        }
        for r in board[offset : offset + limit]
    ]


@router.get("/rankings/exam/{exam_id}")
async def my_exam_rank(
    exam_id: int,
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    """The caller's island and district rank on one exam (null until they've sat it)."""
    assessment = _get_published_or_404(exam_id, db)
    me = int(payload["sub"])
    mine = next(
        (r for r in await _exam_board(exam_id, db) if r["student_id"] == me), None
    )
    title = (await get_course_titles([assessment.course_id])).get(
        assessment.course_id, ""
    )
    return {
        "exam_id": str(assessment.id),
        "course_id": str(assessment.course_id),
        "exam_title": assessment.title,
        "course_title": title,
        "subject": title,
        "overall_rank": mine["rank"] if mine else None,
        "district_rank": mine["district_rank"] if mine else None,
    }


# ---------------------------------------------------------------------------
# Badges (static paths, so they sit above GET /{assessment_id})
# ---------------------------------------------------------------------------


def _user_badges(student_id: int, db: Session) -> list[UserBadgeResponse]:
    rows = (
        db.query(UserBadge, Badge)
        .join(Badge, Badge.id == UserBadge.badge_id)
        .filter(UserBadge.student_id == student_id)
        .order_by(UserBadge.awarded_at.desc())
        .all()
    )
    return [
        UserBadgeResponse(
            id=ub.id,
            badge_id=b.id,
            user_id=ub.student_id,
            awarded_at=ub.awarded_at,
            badge=BadgeResponse.model_validate(b),
        )
        for ub, b in rows
    ]


@router.get("/badges", response_model=list[BadgeResponse])
async def list_badges(db: Session = Depends(get_db)):
    """Every badge that can be earned (public)."""
    return db.query(Badge).order_by(Badge.category, Badge.name).all()


@router.get("/badges/my", response_model=list[UserBadgeResponse])
async def my_badges(
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    return _user_badges(int(payload["sub"]), db)


@router.get("/badges/dashboard", response_model=BadgeDashboardResponse)
async def badge_dashboard(
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    return BadgeDashboardResponse(
        available_badges=db.query(Badge).order_by(Badge.category, Badge.name).all(),
        earned_badges=_user_badges(int(payload["sub"]), db),
    )


@router.get("/my-submissions", response_model=list[MyAttemptItem])
async def list_my_submissions(
    limit: int = Query(10, ge=1, le=50),
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    """The caller's own graded attempts, newest first (My Results)."""
    student_id = int(payload["sub"])
    submissions = (
        db.query(Submission)
        .filter(
            Submission.student_id == student_id,
            Submission.status == SubmissionStatus.graded,
        )
        .order_by(Submission.submitted_at.desc(), Submission.id.desc())
        .limit(limit)
        .all()
    )
    if not submissions:
        return []

    assessment_ids = {sub.assessment_id for sub in submissions}
    assessments = {
        a.id: a
        for a in db.query(Assessment).filter(Assessment.id.in_(assessment_ids)).all()
    }
    questions_by_assessment: dict[int, list[Question]] = {}
    for q in (
        db.query(Question).filter(Question.assessment_id.in_(assessment_ids)).all()
    ):
        questions_by_assessment.setdefault(q.assessment_id, []).append(q)
    course_titles = await get_course_titles([a.course_id for a in assessments.values()])

    # Attempt number = graded attempts on that exam up to and including this one.
    history = (
        db.query(Submission.id, Submission.assessment_id)
        .filter(
            Submission.student_id == student_id,
            Submission.assessment_id.in_(assessment_ids),
            Submission.status == SubmissionStatus.graded,
        )
        .order_by(Submission.submitted_at, Submission.id)
        .all()
    )
    attempt_numbers: dict[int, int] = {}
    seen: dict[int, int] = {}
    for sub_id, a_id in history:
        seen[a_id] = seen.get(a_id, 0) + 1
        attempt_numbers[sub_id] = seen[a_id]

    ranks = {a_id: await _my_ranks(a_id, student_id, db) for a_id in assessments}

    items = []
    for sub in submissions:
        assessment = assessments.get(sub.assessment_id)
        if not assessment:
            continue  # the exam was deleted since
        questions = questions_by_assessment.get(sub.assessment_id, [])
        items.append(
            MyAttemptItem(
                attempt_id=sub.id,
                exam_id=assessment.id,
                attempt_number=attempt_numbers.get(sub.id, 1),
                course_id=assessment.course_id,
                sub_course_id=assessment.module_id,
                exam_title=assessment.title,
                course_title=course_titles.get(assessment.course_id, ""),
                marks_obtained=_correct_count(questions, _answers_of(sub)),
                total_questions=len(questions),
                time_taken_seconds=_time_taken(sub),
                status="submitted",
                started_at=sub.started_at,
                submitted_at=sub.submitted_at,
                overall_rank=ranks[assessment.id][0],
                district_rank=ranks[assessment.id][1],
            )
        )
    return items


@router.get("/submissions/{submission_id}/review", response_model=AttemptReviewResponse)
async def review_submission(
    submission_id: int,
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    """Full Q&A review of one graded attempt, for its student, an admin, or the course teacher."""
    submission = (
        db.query(Submission)
        .filter(
            Submission.id == submission_id, Submission.status == SubmissionStatus.graded
        )
        .first()
    )
    not_found = HTTPException(status_code=404, detail={"error": "ATTEMPT_NOT_FOUND"})
    if not submission:
        raise not_found
    assessment = (
        db.query(Assessment).filter(Assessment.id == submission.assessment_id).first()
    )
    if not assessment:
        raise not_found

    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    if submission.student_id != requester_id:
        if requester_role == "student":
            raise not_found  # never confirm that another student's attempt exists
        await _assert_course_owner(assessment.course_id, requester_id, requester_role)

    questions = (
        db.query(Question)
        .filter(Question.assessment_id == assessment.id)
        .order_by(Question.position, Question.id)
        .all()
    )
    answers = _answers_of(submission)
    score = float(submission.score or 0)
    course_titles = await get_course_titles([assessment.course_id])
    overall_rank, district_rank = await _my_ranks(
        assessment.id, submission.student_id, db
    )
    return AttemptReviewResponse(
        attempt_id=submission.id,
        student_id=submission.student_id,
        score=score,
        passed=score >= assessment.pass_score,
        marks_obtained=_correct_count(questions, answers),
        total_questions=len(questions),
        time_taken_seconds=_time_taken(submission),
        submitted_at=submission.submitted_at,
        review=_build_review(questions, answers),
        ranking=AttemptRanking(
            exam_id=assessment.id,
            course_id=assessment.course_id,
            exam_title=assessment.title,
            course_title=course_titles.get(assessment.course_id, ""),
            overall_rank=overall_rank,
            district_rank=district_rank,
        ),
    )


# Must stay above GET /{assessment_id}, which would otherwise capture it.
@router.get("/my-attempts", response_model=list[MyAttemptSummary])
async def list_my_attempts(
    course_id: int = Query(...),
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    """The caller's own graded attempts, summarised per assessment of one course."""
    pass_scores = dict(
        db.query(Assessment.id, Assessment.pass_score)
        .filter(Assessment.course_id == course_id)
        .all()
    )
    if not pass_scores:
        return []
    submissions = (
        db.query(Submission)
        .filter(
            Submission.student_id == int(payload["sub"]),
            Submission.assessment_id.in_(list(pass_scores)),
            Submission.status == SubmissionStatus.graded,
        )
        .order_by(Submission.submitted_at.desc())
        .all()
    )
    summary: dict[int, MyAttemptSummary] = {}
    for sub in submissions:
        score = float(sub.score or 0)
        passed = score >= pass_scores[sub.assessment_id]
        existing = summary.get(sub.assessment_id)
        if existing is None:
            summary[sub.assessment_id] = MyAttemptSummary(
                assessment_id=sub.assessment_id,
                attempt_count=1,
                last_score=score,
                passed=passed,
            )
        else:
            existing.attempt_count += 1
            existing.passed = existing.passed or passed
    return list(summary.values())


@router.post(
    "/", response_model=AssessmentResponse, status_code=status.HTTP_201_CREATED
)
async def create_assessment(
    body: AssessmentCreate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    await _assert_course_owner(body.course_id, requester_id, requester_role)

    assessment = Assessment(
        course_id=body.course_id,
        module_id=body.module_id,
        lesson_id=body.lesson_id,
        title=body.title,
        description=body.description,
        assessment_type=AssessmentType(body.assessment_type),
        time_limit_minutes=body.time_limit_minutes,
        max_score=body.max_score,
        pass_score=body.pass_score,
        is_published=body.is_published,
        image_url=body.image_url,
        image_public_id=body.image_public_id,
        position=body.position,
        scheduled_start=_as_utc(body.scheduled_start),
    )
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    return _to_response(assessment, 0)


@router.delete("/by-module/{module_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_module_assessments(
    module_id: int,
    course_id: int = Query(...),
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    """Removes every exam (and its questions) inside one lesson — called before the lesson itself is deleted."""
    await _assert_course_owner(
        course_id, int(payload["sub"]), payload.get("role", "").lower()
    )
    ids = [
        row.id
        for row in db.query(Assessment.id).filter(
            Assessment.course_id == course_id, Assessment.module_id == module_id
        )
    ]
    if ids:
        db.query(Question).filter(Question.assessment_id.in_(ids)).delete(
            synchronize_session=False
        )
        db.query(Assessment).filter(Assessment.id.in_(ids)).delete(
            synchronize_session=False
        )
        db.commit()


@router.put("/{assessment_id}", response_model=AssessmentResponse)
async def update_assessment(
    assessment_id: int,
    body: AssessmentUpdate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    assessment = _get_assessment_or_404(assessment_id, db)
    await _assert_course_owner(assessment.course_id, requester_id, requester_role)

    updates = body.model_dump(exclude_unset=True)
    if "assessment_type" in updates:
        updates["assessment_type"] = AssessmentType(updates["assessment_type"])
    if "scheduled_start" in updates:
        updates["scheduled_start"] = _as_utc(updates["scheduled_start"])
    for field, value in updates.items():
        setattr(assessment, field, value)

    db.commit()
    db.refresh(assessment)
    return _to_response(assessment, db=db)


@router.delete("/{assessment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assessment(
    assessment_id: int,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    assessment = _get_assessment_or_404(assessment_id, db)
    await _assert_course_owner(assessment.course_id, requester_id, requester_role)

    db.query(Question).filter(Question.assessment_id == assessment_id).delete()
    db.delete(assessment)
    db.commit()


# ---------------------------------------------------------------------------
# Question management endpoints (teacher/admin authoring)
# ---------------------------------------------------------------------------

_MAX_CSV_SIZE = 2 * 1024 * 1024  # 2 MB

_CSV_COLUMNS = (
    "question_text",
    "option_a",
    "option_b",
    "option_c",
    "option_d",
    "option_e",
    "correct_answer",
    "explanation",
)

_CSV_TEMPLATE_ROWS = (
    (
        "What is the capital of France?",
        "London",
        "Paris",
        "Berlin",
        "Madrid",
        "",
        "B",
        "Paris is the capital and largest city of France.",
    ),
    (
        "Which planet is known as the Red Planet?",
        "Venus",
        "Mars",
        "Jupiter",
        "Saturn",
        "",
        "B",
        "Mars looks red because of iron oxide on its surface.",
    ),
    (
        "What is the derivative of x^2?",
        "x",
        "2x",
        "x^2",
        "2",
        "0",
        "B",
        "By the power rule, d/dx(x^2) = 2x.",
    ),
)


@router.get("/questions/csv-template")
async def download_questions_csv_template(
    _: dict = Depends(require_role(_TEACHER_ADMIN)),
):
    """A ready-to-fill CSV showing the columns the importer expects."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    writer.writerows(_CSV_TEMPLATE_ROWS)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="questions_template.csv"'
        },
    )


@router.get("/{assessment_id}/questions", response_model=list[QuestionAdminResponse])
async def list_questions(
    assessment_id: int,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    """Full question detail (including correct answers) for the owning teacher/admin."""
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    assessment = _get_assessment_or_404(assessment_id, db)
    await _assert_course_owner(assessment.course_id, requester_id, requester_role)

    questions = (
        db.query(Question)
        .filter(Question.assessment_id == assessment_id)
        .order_by(Question.position)
        .all()
    )
    return [_question_to_admin_response(q) for q in questions]


@router.post(
    "/{assessment_id}/questions",
    response_model=QuestionAdminResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_question(
    assessment_id: int,
    body: QuestionCreate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    assessment = _get_assessment_or_404(assessment_id, db)
    await _assert_course_owner(assessment.course_id, requester_id, requester_role)

    option_texts = [o.option_text for o in body.options]
    correct_option = next((o.option_text for o in body.options if o.is_correct), None)
    correct_answer = body.correct_answer or correct_option
    if not correct_answer:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "CORRECT_ANSWER_REQUIRED",
                "message": "Provide correct_answer, or mark one option as is_correct",
            },
        )

    question = Question(
        assessment_id=assessment_id,
        question_text=body.question_text,
        question_type=QuestionType(body.question_type),
        options_json=json.dumps(option_texts) if option_texts else None,
        option_images_json=_option_images_json(body.options),
        correct_answer=correct_answer,
        explanation=body.explanation,
        question_image_url=body.question_image_url,
        question_image_public_id=body.question_image_public_id,
        marks=body.marks,
        position=body.position,
    )
    db.add(question)
    db.commit()
    db.refresh(question)
    return _question_to_admin_response(question)


@router.post(
    "/{assessment_id}/questions/import-csv",
    response_model=CsvImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def import_questions_csv(
    assessment_id: int,
    file: UploadFile = File(...),
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Appends MCQ questions from a CSV in the /questions/csv-template shape.
    Rows are independent: a bad row is reported and skipped, the rest import.
    """
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    assessment = _get_assessment_or_404(assessment_id, db)
    await _assert_course_owner(assessment.course_id, requester_id, requester_role)

    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(
            status_code=415,
            detail={"error": "NOT_A_CSV", "message": "File must be a .csv"},
        )

    raw = await file.read()
    if len(raw) > _MAX_CSV_SIZE:
        raise HTTPException(
            status_code=413,
            detail={"error": "FILE_TOO_LARGE", "message": "CSV exceeds the 2 MB limit"},
        )
    try:
        # utf-8-sig drops the BOM Excel writes, which would otherwise corrupt
        # the first column name and make every row look like it lacks a question.
        text_content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "INVALID_ENCODING",
                "message": "CSV must be UTF-8 encoded",
            },
        )

    created, errors = import_question_rows(db, assessment_id, text_content)
    if created:
        db.commit()

    return CsvImportResult(
        message=f"Imported {created} question(s)",
        questions_created=created,
        errors=errors,
    )


_MAX_ZIP_SIZE = 100 * 1024 * 1024  # 100 MB


async def _read_zip(file: UploadFile) -> bytes:
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(
            status_code=415,
            detail={"error": "NOT_A_ZIP", "message": "Please upload a .zip file"},
        )
    data = await file.read()
    if len(data) > _MAX_ZIP_SIZE:
        raise HTTPException(
            status_code=413,
            detail={"error": "FILE_TOO_LARGE", "message": "ZIP must be 100 MB or less"},
        )
    return data


@router.post("/{assessment_id}/questions/images-zip")
async def upload_question_images_zip(
    assessment_id: int,
    file: UploadFile = File(...),
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    """Attach images named {question_number}-{n}.png (n = 0 question, 1-5 options A-E)."""
    assessment = _get_assessment_or_404(assessment_id, db)
    await _assert_course_owner(
        assessment.course_id, int(payload["sub"]), payload.get("role", "").lower()
    )
    try:
        zf = safe_zip(await _read_zip(file))
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail={"error": "INVALID_ZIP", "message": str(e)}
        )
    images = [
        (info.filename, zf.read(info)) for info in zf.infolist() if not info.is_dir()
    ]
    applied, skipped, errors = await apply_question_images(db, assessment_id, images)
    db.commit()
    return {
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "message": f"Applied {applied} image(s)"
        + (f", skipped {skipped} file(s) with other names" if skipped else ""),
    }


@router.get("/bulk-import/template")
async def bulk_import_template(_: dict = Depends(require_role(_TEACHER_ADMIN))):
    return Response(
        content=template_zip(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="bulk_exam_template.zip"'
        },
    )


@router.post("/bulk-import")
async def bulk_import_exams(
    course_id: int = Query(...),
    module_id: int | None = Query(None, description="The lesson the exams go into"),
    file: UploadFile = File(...),
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    """Create several exams (with questions, images and thumbnails) from one ZIP bundle."""
    await _assert_course_owner(
        course_id, int(payload["sub"]), payload.get("role", "").lower()
    )
    data = await _read_zip(file)
    try:
        return await bulk_import(db, course_id, module_id, data)
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=422, detail={"error": "INVALID_BUNDLE", "message": str(e)}
        )


@router.put("/questions/{question_id}", response_model=QuestionAdminResponse)
async def update_question(
    question_id: int,
    body: QuestionUpdate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    question = _get_question_or_404(question_id, db)
    assessment = _get_assessment_or_404(question.assessment_id, db)
    await _assert_course_owner(assessment.course_id, requester_id, requester_role)

    updates = body.model_dump(exclude_unset=True)
    if "options" in updates:
        options = updates.pop("options") or []
        question.options_json = (
            json.dumps([o["option_text"] for o in options]) if options else None
        )
        question.option_images_json = _option_images_json(body.options or [])
        correct_from_options = next(
            (o["option_text"] for o in options if o["is_correct"]), None
        )
        if correct_from_options and "correct_answer" not in updates:
            question.correct_answer = correct_from_options
    if "question_type" in updates:
        updates["question_type"] = QuestionType(updates["question_type"])
    for field, value in updates.items():
        setattr(question, field, value)

    db.commit()
    db.refresh(question)
    return _question_to_admin_response(question)


@router.delete("/questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_question(
    question_id: int,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    question = _get_question_or_404(question_id, db)
    assessment = _get_assessment_or_404(question.assessment_id, db)
    await _assert_course_owner(assessment.course_id, requester_id, requester_role)

    db.delete(question)
    db.commit()


# ---------------------------------------------------------------------------
# Student-facing assessment endpoints
# ---------------------------------------------------------------------------


@router.get("/{assessment_id}", response_model=AssessmentResponse)
@router.get("/api/assessments/{assessment_id}", response_model=AssessmentResponse)
async def get_assessment(
    assessment_id: int,
    db: Session = Depends(get_db),
    payload: dict = Depends(require_role(_ALL_ROLES)),
):
    """
    Get assessment metadata by ID.
    """
    assessment = _get_assessment_or_404(assessment_id, db)
    if _is_public_viewer(payload) and not assessment.is_published:
        raise HTTPException(
            status_code=404,
            detail={"error": "ASSESSMENT_NOT_FOUND", "message": "Assessment not found"},
        )
    return _to_response(assessment, db=db)


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

    assessment = _get_assessment_or_404(assessment_id, db)
    if _is_public_viewer(payload):
        if not assessment.is_published:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "ASSESSMENT_NOT_FOUND",
                    "message": "Assessment not found",
                },
            )
        if not await is_enrolled(
            student_id, assessment.course_id, assessment.module_id
        ):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "NOT_ENROLLED",
                    "message": "Enroll in this course to take this exam",
                },
            )
        starts_at = _as_utc(assessment.scheduled_start)
        if starts_at and datetime.now(timezone.utc) < starts_at:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "EXAM_NOT_STARTED",
                    "not_started_yet": True,
                    "message": "This exam has not started yet",
                    "scheduled_start": starts_at.isoformat(),
                },
            )

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
            question_image_url=q.question_image_url,
            option_image_urls=[
                img["url"] if img else None
                for img in _option_images(q, len(json.loads(q.options_json or "[]")))
            ],
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
async def submit_assessment(  # noqa: C901 - grading, session cleanup, badges and the response in one request
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
    submission.answers_json = json.dumps([a.model_dump() for a in body.answers])
    submission.submitted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(submission)

    # Clear Redis keys
    redis_c.delete(session_key)
    redis_c.delete(active_key)

    # Publish RabbitMQ event
    publish_assessment_graded(
        student_id=student_id,
        assessment_id=assessment_id,
        score=score,
        passed=passed,
    )

    time_taken = 0
    if submission.started_at and submission.submitted_at:
        time_taken = int(
            (submission.submitted_at - submission.started_at).total_seconds()
        )

    overall_rank, district_rank = await _my_ranks(assessment_id, student_id, db)

    new_badges: list[Badge] = []
    try:
        new_badges = evaluate_after_attempt(db, submission, assessment)
    except Exception as e:  # a badge hiccup must never fail the submit itself
        db.rollback()
        logger.error("Badge evaluation failed for submission %s: %s", submission.id, e)

    return SubmitAssessmentResponse(
        session_id=body.session_id,
        new_badges=[BadgeResponse.model_validate(b) for b in new_badges],
        score=score,
        passed=passed,
        correct_count=correct_count,
        total_questions=total_questions,
        auto_submitted=auto_submitted,
        time_taken_seconds=time_taken,
        review=_build_review(questions, answers_dict),
        ranking=SubmissionRanking(
            exam_id=assessment_id,
            course_id=assessment.course_id,
            exam_title=assessment.title,
            overall_rank=overall_rank,
            district_rank=district_rank,
        ),
    )


@router.get("/{assessment_id}/my-last-attempt", response_model=SubmitAssessmentResponse)
@router.get(
    "/api/assessments/{assessment_id}/my-last-attempt",
    response_model=SubmitAssessmentResponse,
)
async def get_my_last_attempt(
    assessment_id: int,
    db: Session = Depends(get_db),
    payload: dict = Depends(require_role(_ALL_ROLES)),
):
    """
    Re-fetches a student's own most recent graded attempt on this assessment
    — lets them navigate back to their results (e.g. via a "My Results" list)
    without needing to keep the submit response around client-side, and is
    what a ?view=results deep link resolves against.
    """
    student_id = int(payload["sub"])
    assessment = _get_assessment_or_404(assessment_id, db)

    submission = (
        db.query(Submission)
        .filter(
            Submission.assessment_id == assessment_id,
            Submission.student_id == student_id,
            Submission.status == SubmissionStatus.graded,
        )
        .order_by(Submission.submitted_at.desc())
        .first()
    )
    if not submission:
        raise HTTPException(status_code=404, detail={"error": "NO_ATTEMPT_FOUND"})

    questions = db.query(Question).filter(Question.assessment_id == assessment_id).all()
    answers_dict = {}
    if submission.answers_json:
        for a in json.loads(submission.answers_json):
            answers_dict[a["question_id"]] = a["selected_option"]

    correct_count = sum(
        1
        for q in questions
        if q.question_type in (QuestionType.mcq, QuestionType.true_false)
        and answers_dict.get(q.id, "").strip().lower()
        == str(q.correct_answer).strip().lower()
    )

    time_taken = 0
    if submission.started_at and submission.submitted_at:
        time_taken = int(
            (submission.submitted_at - submission.started_at).total_seconds()
        )

    overall_rank, district_rank = await _my_ranks(assessment_id, student_id, db)

    return SubmitAssessmentResponse(
        session_id="",
        score=float(submission.score or 0),
        passed=float(submission.score or 0) >= assessment.pass_score,
        correct_count=correct_count,
        total_questions=len(questions),
        auto_submitted=False,
        time_taken_seconds=time_taken,
        review=_build_review(questions, answers_dict),
        ranking=SubmissionRanking(
            exam_id=assessment_id,
            course_id=assessment.course_id,
            exam_title=assessment.title,
            overall_rank=overall_rank,
            district_rank=district_rank,
        ),
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
