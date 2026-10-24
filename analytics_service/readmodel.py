"""The data reports are built from, read from this service's own tables.

Same function names the progress and dashboard code always used, but every one
now queries the replica tables that ``event_consumer`` keeps current from other
services' events. Nothing here makes a network call.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from database import SessionLocal
from fastapi.concurrency import run_in_threadpool
from models import (
    ReplicaAccess,
    ReplicaAttempt,
    ReplicaBadge,
    ReplicaCourse,
    ReplicaEnrollment,
    ReplicaExam,
    ReplicaModule,
    ReplicaPayment,
    ReplicaReceipt,
    ReplicaUser,
)
from sqlalchemy import func

from shared.replicas import accessible as open_access

SL_TIME = timezone(timedelta(hours=5, minutes=30))


class SourceUnavailable(Exception):
    """The read model could not be queried (the database is down)."""


def _run(fn, *args):
    """Run a blocking query on a worker thread with its own session."""

    def call():
        db = SessionLocal()
        try:
            return fn(db, *args)
        except Exception as exc:
            raise SourceUnavailable(str(exc)) from exc
        finally:
            db.close()

    return run_in_threadpool(call)


def _local(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(SL_TIME)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _course_dict(c: ReplicaCourse) -> dict:
    return {
        "id": c.id,
        "title": c.title,
        "grade": c.grade,
        "stream_ids": c.stream_ids or [],
        "course_type": c.course_type or "video",
        "status": c.status,
        "price": float(c.price or 0),
        "thumbnail_url": c.thumbnail_url,
    }


# ---------------------------------------------------------------- people


def _profile(db, user_id: int):
    u = db.get(ReplicaUser, user_id)
    if u is None or u.full_name is None:
        return None
    return {
        "user_id": u.user_id,
        "role": u.role or "",
        "full_name": u.full_name,
        "grade": u.grade,
        "school": u.school,
        "district": u.district,
        "mobile_no": u.mobile_no,
    }


async def profile(user_id: int) -> dict | None:
    return await _run(_profile, user_id)


def _account(db, user_id: int):
    u = db.get(ReplicaUser, user_id)
    return {"id": u.user_id, "email": u.email} if u and u.email else None


async def account(user_id: int) -> dict | None:
    return await _run(_account, user_id)


def _students(db, limit: int):
    rows = (
        db.query(ReplicaUser)
        .filter(ReplicaUser.role == "student", ReplicaUser.full_name.isnot(None))
        .order_by(ReplicaUser.full_name)
        .limit(limit)
        .all()
    )
    return [
        {
            "user_id": str(u.user_id),
            "full_name": u.full_name,
            "grade": u.grade,
            "district": u.district,
            "school": u.school,
            "mobile_no": u.mobile_no,
        }
        for u in rows
    ]


async def students(limit: int = 500) -> list[dict]:
    return await _run(_students, limit)


def _names(db, user_ids):
    rows = db.query(ReplicaUser).filter(ReplicaUser.user_id.in_(list(user_ids))).all()
    return {r.user_id: r.display_name for r in rows if r.display_name}


async def names(user_ids) -> dict[int, str]:
    return await _run(_names, set(user_ids)) if user_ids else {}


# ---------------------------------------------------------------- courses


def _course_titles(db, course_ids):
    rows = db.query(ReplicaCourse).filter(ReplicaCourse.id.in_(list(course_ids))).all()
    return {r.id: r.title for r in rows}


async def course_titles(course_ids) -> dict[int, str]:
    return await _run(_course_titles, set(course_ids)) if course_ids else {}


def _course_info(db, course_id: int):
    c = db.get(ReplicaCourse, course_id)
    return _course_dict(c) if c else None


async def course_info(course_id: int) -> dict | None:
    return await _run(_course_info, course_id)


def _course_modules(db, course_id: int):
    if db.get(ReplicaCourse, course_id) is None:
        return None
    rows = (
        db.query(ReplicaModule)
        .filter(ReplicaModule.course_id == course_id)
        .order_by(ReplicaModule.position, ReplicaModule.id)
        .all()
    )
    return [{"id": m.id, "title": m.title, "order": m.position} for m in rows]


async def course_modules(course_id: int) -> list[dict] | None:
    return await _run(_course_modules, course_id)


# ---------------------------------------------------------------- one student's progress


def _accessible(db, user_id: int):
    courses, lessons = open_access(db.get(ReplicaAccess, user_id))
    return {"course_ids": sorted(courses), "lesson_ids": sorted(lessons)}


async def accessible(user_id: int) -> dict:
    return await _run(_accessible, user_id)


def _exam_progress(db, user_id: int, course_ids, lesson_ids):
    q = db.query(ReplicaExam).filter(ReplicaExam.is_published.is_(True))
    if course_ids and lesson_ids:
        q = q.filter(
            ReplicaExam.course_id.in_(course_ids)
            | ReplicaExam.module_id.in_(lesson_ids)
        )
    elif course_ids:
        q = q.filter(ReplicaExam.course_id.in_(course_ids))
    else:
        q = q.filter(ReplicaExam.module_id.in_(lesson_ids))
    exams = q.order_by(
        ReplicaExam.course_id, ReplicaExam.position, ReplicaExam.id
    ).all()
    if not exams:
        return []
    attempts: dict[int, list[dict]] = defaultdict(list)
    rows = (
        db.query(ReplicaAttempt)
        .filter(
            ReplicaAttempt.student_id == user_id,
            ReplicaAttempt.exam_id.in_([e.id for e in exams]),
        )
        .order_by(ReplicaAttempt.submitted_at, ReplicaAttempt.submission_id)
        .all()
    )
    for a in rows:
        attempts[a.exam_id].append(
            {
                "id": a.submission_id,
                "score_pct": round(a.score or 0),
                "marks_obtained": a.marks_obtained,
                "total_questions": a.total_questions,
                "time_seconds": a.time_seconds,
                "submitted_at": _iso(a.submitted_at),
            }
        )
    return [
        {
            "id": e.id,
            "title": e.title,
            "course_id": e.course_id,
            "module_id": e.module_id,
            "order_number": e.position or 0,
            "attempts": attempts.get(e.id, []),
        }
        for e in exams
    ]


async def exam_progress(user_id: int, course_ids: list[int], lesson_ids: list[int]):
    if not course_ids and not lesson_ids:
        return []
    return await _run(_exam_progress, user_id, course_ids, lesson_ids)


def _badge_count(db, user_id: int) -> int:
    return db.query(ReplicaBadge).filter(ReplicaBadge.student_id == user_id).count()


async def badge_count(user_id: int) -> int:
    return await _run(_badge_count, user_id)


# ---------------------------------------------------------------- one course


def _first_attempts(db, course_id: int):
    exams = db.query(ReplicaExam).filter(ReplicaExam.course_id == course_id).all()
    if not exams:
        return {"exams": [], "attempts": []}
    first: dict[tuple[int, int], ReplicaAttempt] = {}
    for a in (
        db.query(ReplicaAttempt)
        .filter(ReplicaAttempt.exam_id.in_([e.id for e in exams]))
        .order_by(ReplicaAttempt.submitted_at, ReplicaAttempt.submission_id)
    ):
        first.setdefault((a.student_id, a.exam_id), a)
    return {
        "exams": [
            {"id": e.id, "title": e.title, "module_id": e.module_id} for e in exams
        ],
        "attempts": [
            {"student_id": s, "exam_id": e, "score_pct": float(a.score or 0)}
            for (s, e), a in first.items()
        ],
    }


async def first_attempts(course_id: int) -> dict:
    return await _run(_first_attempts, course_id)


# ---------------------------------------------------------------- admin dashboard and public stats


def _enrollment_counts(db):
    rows = (
        db.query(ReplicaEnrollment.course_id, func.count())
        .filter(ReplicaEnrollment.status == "ACTIVE")
        .group_by(ReplicaEnrollment.course_id)
        .all()
    )
    return [{"course_id": cid, "count": n} for cid, n in rows]


def _published_courses(db):
    rows = db.query(ReplicaCourse).order_by(ReplicaCourse.id).all()
    return [_course_dict(c) for c in rows if c.is_published]


def _platform_sources(db):
    return {
        "users": {
            "total_students": db.query(ReplicaUser)
            .filter(ReplicaUser.role == "student", ReplicaUser.full_name.isnot(None))
            .count()
        },
        "courses": _published_courses(db),
        "enrollments": _enrollment_counts(db),
        "assessments": {
            "published_exams": db.query(ReplicaExam)
            .filter(ReplicaExam.is_published.is_(True))
            .count(),
            "graded_attempts": db.query(ReplicaAttempt).count(),
        },
    }


async def fetch_platform_sources() -> dict:
    return await _run(_platform_sources)


def _users_for_admin(db):
    students_q = db.query(ReplicaUser).filter(
        ReplicaUser.role == "student", ReplicaUser.full_name.isnot(None)
    )
    by_grade = (
        db.query(ReplicaUser.grade, func.count())
        .filter(
            ReplicaUser.role == "student",
            ReplicaUser.full_name.isnot(None),
            ReplicaUser.grade.isnot(None),
        )
        .group_by(ReplicaUser.grade)
        .order_by(ReplicaUser.grade)
        .all()
    )
    by_district = (
        db.query(ReplicaUser.district, func.count())
        .filter(
            ReplicaUser.role == "student",
            ReplicaUser.full_name.isnot(None),
            ReplicaUser.district.isnot(None),
            ReplicaUser.district != "",
        )
        .group_by(ReplicaUser.district)
        .order_by(func.count().desc(), ReplicaUser.district)
        .limit(10)
        .all()
    )
    recent = (
        students_q.order_by(
            ReplicaUser.profile_created_at.desc(), ReplicaUser.user_id.desc()
        )
        .limit(5)
        .all()
    )
    return {
        "total_students": students_q.count(),
        "by_grade": [{"grade": g, "count": n} for g, n in by_grade],
        "by_district": [{"district": d, "count": n} for d, n in by_district],
        "recent_students": [
            {
                "user_id": u.user_id,
                "full_name": u.full_name,
                "district": u.district,
                "created_at": _iso(u.profile_created_at),
            }
            for u in recent
        ],
    }


def _assessments_for_admin(db):
    months: dict[tuple[int, int], int] = defaultdict(int)
    for (when,) in db.query(ReplicaAttempt.submitted_at).filter(
        ReplicaAttempt.submitted_at.isnot(None)
    ):
        local = _local(when)
        months[(local.year, local.month)] += 1

    top = (
        db.query(ReplicaAttempt.exam_id, func.count(), func.avg(ReplicaAttempt.score))
        .group_by(ReplicaAttempt.exam_id)
        .order_by(func.count().desc())
        .limit(8)
        .all()
    )
    exams = (
        {
            e.id: e
            for e in db.query(ReplicaExam)
            .filter(ReplicaExam.id.in_([row[0] for row in top]))
            .all()
        }
        if top
        else {}
    )
    recent = (
        db.query(ReplicaExam)
        .order_by(ReplicaExam.created_at.desc(), ReplicaExam.id.desc())
        .limit(5)
        .all()
    )
    return {
        "published_exams": db.query(ReplicaExam)
        .filter(ReplicaExam.is_published.is_(True))
        .count(),
        "graded_attempts": db.query(ReplicaAttempt).count(),
        "attempts_by_month": [
            {"year": y, "month": m, "count": months[(y, m)]}
            for (y, m) in sorted(months)[-12:]
        ],
        "top_exams": [
            {
                "exam_id": exam_id,
                "title": exams[exam_id].title,
                "course_id": exams[exam_id].course_id,
                "attempt_count": count,
                "avg_score": round(float(avg or 0), 1),
            }
            for exam_id, count, avg in top
            if exam_id in exams
        ],
        "recent_exams": [
            {
                "title": e.title,
                "course_id": e.course_id,
                "created_at": _iso(e.created_at),
            }
            for e in recent
        ],
    }


def _payments_for_admin(db):
    paid = (
        db.query(ReplicaPayment.amount, ReplicaPayment.completed_at)
        .filter(ReplicaPayment.amount > 0)
        .all()
    )
    months: dict[tuple[int, int], list] = {}
    for amount, when in paid:
        if when is None:
            continue
        local = _local(when)
        bucket = months.setdefault((local.year, local.month), [0.0, 0])
        bucket[0] += float(amount)
        bucket[1] += 1
    now = datetime.now(SL_TIME)
    recent = (
        db.query(ReplicaReceipt)
        .filter(
            ReplicaReceipt.status == "APPROVED", ReplicaReceipt.reviewed_at.isnot(None)
        )
        .order_by(ReplicaReceipt.reviewed_at.desc())
        .limit(5)
        .all()
    )
    return {
        "total_revenue": round(sum(float(a) for a, _ in paid), 2),
        "revenue_this_month": round(months.get((now.year, now.month), [0.0])[0], 2),
        "pending_bank_slips": db.query(ReplicaReceipt)
        .filter(ReplicaReceipt.status == "PENDING")
        .count(),
        "revenue_by_month": [
            {
                "year": y,
                "month": m,
                "revenue": round(months[(y, m)][0]),
                "count": months[(y, m)][1],
            }
            for (y, m) in sorted(months)[-12:]
        ],
        "recent_verified": [
            {"student_id": r.student_id, "verified_at": _iso(r.reviewed_at)}
            for r in recent
        ],
    }


def _admin_sources(db):
    return {
        "users": _users_for_admin(db),
        "courses": _published_courses(db),
        "enrollments": _enrollment_counts(db),
        "assessments": _assessments_for_admin(db),
        "payments": _payments_for_admin(db),
    }


async def admin_sources() -> dict:
    """Everything the admin dashboard and analytics page are built from."""
    return await _run(_admin_sources)
