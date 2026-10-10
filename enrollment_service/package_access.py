"""
Who may open what: a course enrollment, a single-lesson grant, or an active
class package that covers the course (the reference platform's rules).
"""

from datetime import datetime, timedelta, timezone

from models import ClassEnrollment, ClassPackage, Enrollment, EnrollmentStatus, LessonAccess
from service_clients import course_info
from sqlalchemy.orm import Session

PACKAGE_DURATION = timedelta(days=365)


def package_covers(package_grade: int, package_stream_id: int | None, course: dict) -> bool:
    """grade 5-10 -> same grade; 11 -> 10 & 11; 12 -> 12 in stream; 13 -> 12 & 13 in stream."""
    course_grade = course.get("grade")
    if course_grade is None:
        return False
    if 5 <= package_grade <= 10:
        return course_grade == package_grade
    if package_grade == 11:
        return course_grade in (10, 11)
    if package_grade in (12, 13):
        allowed = (12,) if package_grade == 12 else (12, 13)
        if course_grade not in allowed:
            return False
        streams = course.get("stream_ids") or []
        # A course without streams is open to every A/L stream.
        return package_stream_id is None or not streams or package_stream_id in streams
    return False


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def active_packages(db: Session, user_id: int) -> list[ClassPackage]:
    now = datetime.now(timezone.utc)
    rows = (
        db.query(ClassEnrollment, ClassPackage)
        .join(ClassPackage, ClassPackage.id == ClassEnrollment.class_package_id)
        .filter(
            ClassEnrollment.user_id == user_id,
            ClassEnrollment.status == "active",
            ClassPackage.is_active.is_(True),
        )
        .all()
    )
    return [pkg for enr, pkg in rows if enr.expires_at is None or _aware(enr.expires_at) > now]


def direct_course_access(db: Session, student_id: int, course_id: int) -> bool:
    return (
        db.query(Enrollment.id)
        .filter(
            Enrollment.student_id == student_id,
            Enrollment.course_id == course_id,
            Enrollment.status == EnrollmentStatus.ACTIVE,
        )
        .first()
        is not None
    )


def lesson_grants(db: Session, student_id: int, course_id: int) -> list[int]:
    return [
        module_id
        for (module_id,) in db.query(LessonAccess.module_id).filter(
            LessonAccess.student_id == student_id, LessonAccess.course_id == course_id
        )
    ]


async def course_access(db: Session, student_id: int, course_id: int) -> tuple[bool, str | None]:
    """(whole course unlocked?, how) — how is 'course' or 'class'."""
    if direct_course_access(db, student_id, course_id):
        return True, "course"
    packages = active_packages(db, student_id)
    if packages:
        course = await course_info(course_id)
        if course and any(package_covers(p.grade, p.stream_id, course) for p in packages):
            return True, "class"
    return False, None


async def has_access(db: Session, student_id: int, course_id: int, module_id: int | None) -> bool:
    whole_course, _ = await course_access(db, student_id, course_id)
    if whole_course:
        return True
    return module_id is not None and module_id in lesson_grants(db, student_id, course_id)


def grant_package(db: Session, user_id: int, package_id: int, payment_id: int | None = None,
                  expires_at: datetime | None = None) -> ClassEnrollment:
    """Create or reactivate a student's package for a year (or until expires_at). Caller commits."""
    until = expires_at or datetime.now(timezone.utc) + PACKAGE_DURATION
    enrollment = (
        db.query(ClassEnrollment)
        .filter(ClassEnrollment.user_id == user_id, ClassEnrollment.class_package_id == package_id)
        .first()
    )
    if enrollment is None:
        enrollment = ClassEnrollment(
            user_id=user_id, class_package_id=package_id, payment_id=payment_id,
            status="active", expires_at=until,
        )
        db.add(enrollment)
    else:
        enrollment.status = "active"
        enrollment.payment_id = payment_id
        enrollment.enrolled_at = datetime.now(timezone.utc)
        enrollment.expires_at = until
    return enrollment
