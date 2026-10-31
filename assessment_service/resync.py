"""Re-announce this service's data so other services' copies (and analytics) catch up."""

from database import SessionLocal
from events import (
    announce_assessment,
    announce_badge,
    publish_assessment_graded,
)
from models import Assessment, Badge, Question, Submission, SubmissionStatus, UserBadge
from router import _answers_of, _correct_count, _time_taken


def announce_everything() -> None:
    db = SessionLocal()
    try:
        assessments = {a.id: a for a in db.query(Assessment).all()}
        for a in assessments.values():
            announce_assessment(a)

        keys = {b.id: b.criteria_key for b in db.query(Badge).all()}
        for ub in db.query(UserBadge).all():
            announce_badge(ub.student_id, keys.get(ub.badge_id, ""), ub.awarded_at)

        questions: dict[int, list[Question]] = {}
        for q in db.query(Question).all():
            questions.setdefault(q.assessment_id, []).append(q)
        for sub in db.query(Submission).filter(
            Submission.status == SubmissionStatus.graded
        ):
            a = assessments.get(sub.assessment_id)
            if a is None:
                continue
            qs = questions.get(a.id, [])
            score = float(sub.score or 0)
            publish_assessment_graded(
                student_id=sub.student_id,
                assessment_id=a.id,
                score=score,
                passed=score >= a.pass_score,
                course_id=a.course_id,
                module_id=a.module_id,
                submission_id=sub.id,
                marks_obtained=_correct_count(qs, _answers_of(sub)),
                total_questions=len(qs),
                time_seconds=_time_taken(sub),
                submitted_at=sub.submitted_at,
            )
    finally:
        db.close()
