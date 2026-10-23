"""Announcing who can open what.

Whenever a student's access could have changed, the full picture for that
student is published as ``access.changed``. course_service, content_service,
assessment_service and analytics_service keep it in their own copy, so none of
them has to ask this service whether a student may open something.
"""

from models import ClassEnrollment, Enrollment, EnrollmentStatus, LessonAccess
from package_access import (
    active_package_grants,
    package_covers,
    published_courses,
)
from sqlalchemy.orm import Session

from shared import events as ev
from shared.messaging import publish


def compute_access(db: Session, student_id: int) -> dict:
    """Courses open in full (with an end date when a class package gave them) and single lessons."""
    grants: dict[int, str | None] = {}
    for (course_id,) in db.query(Enrollment.course_id).filter(
        Enrollment.student_id == student_id,
        Enrollment.status == EnrollmentStatus.ACTIVE,
    ):
        grants[course_id] = None  # a direct enrollment has no end date here

    package_grants = active_package_grants(db, student_id)
    if package_grants:
        for course in published_courses(db):
            if course["id"] in grants:
                continue
            for package, until in package_grants:
                if package_covers(package.grade, package.stream_id, course):
                    grants[course["id"]] = until.isoformat() if until else None
                    break

    lessons = [
        module_id
        for (course_id, module_id) in db.query(
            LessonAccess.course_id, LessonAccess.module_id
        ).filter(LessonAccess.student_id == student_id)
        if course_id not in grants
    ]
    return {
        "student_id": student_id,
        "courses": [
            {"id": cid, "until": until} for cid, until in sorted(grants.items())
        ],
        "lessons": sorted(lessons),
    }


def announce_access(db: Session, student_id: int) -> None:
    publish(ev.ACCESS_CHANGED, compute_access(db, student_id))


def announce_access_many(db: Session, student_ids) -> None:
    for student_id in sorted(set(student_ids)):
        announce_access(db, student_id)


def announce_enrollment(enrollment: Enrollment, source: str) -> None:
    """A course enrollment was created, renewed, suspended or cancelled."""
    publish(
        ev.ENROLLMENT_CHANGED,
        {
            "student_id": enrollment.student_id,
            "course_id": enrollment.course_id,
            "status": enrollment.status.value,
            "source": source,
        },
    )


def students_with_packages(db: Session) -> set[int]:
    return {
        user_id
        for (user_id,) in db.query(ClassEnrollment.user_id).filter(
            ClassEnrollment.status == "active"
        )
    }


def announce_everything(db: Session) -> None:
    """Re-announce every student's access and enrollment status so other services' copies catch up."""
    students = students_with_packages(db)
    students |= {s for (s,) in db.query(Enrollment.student_id)}
    students |= {s for (s,) in db.query(LessonAccess.student_id)}
    announce_access_many(db, students)
    for enrollment in db.query(Enrollment).all():
        announce_enrollment(enrollment, "resync")
