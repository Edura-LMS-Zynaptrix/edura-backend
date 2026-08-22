from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from models import Enrollment, EnrollmentStatus


def activate_or_extend_enrollment(
    db: Session, student_id: int, course_id: int, extension_days: int = 30
) -> Enrollment:
    now = datetime.now(timezone.utc)
    enrollment = (
        db.query(Enrollment)
        .filter(Enrollment.student_id == student_id, Enrollment.course_id == course_id)
        .first()
    )

    if enrollment:
        if enrollment.status == EnrollmentStatus.ACTIVE and enrollment.expires_at:
            # Enforce AC4: Extend existing active enrollment by 30 days
            # Ensure base datetime is timezone-aware
            base_exp = enrollment.expires_at
            if base_exp.tzinfo is None:
                base_exp = base_exp.replace(tzinfo=timezone.utc)
            if base_exp < now:
                base_exp = now
            enrollment.expires_at = base_exp + timedelta(days=extension_days)
        else:
            enrollment.status = EnrollmentStatus.ACTIVE
            enrollment.enrolled_at = now
            enrollment.expires_at = now + timedelta(days=extension_days)
    else:
        enrollment = Enrollment(
            student_id=student_id,
            course_id=course_id,
            status=EnrollmentStatus.ACTIVE,
            enrolled_at=now,
            expires_at=now + timedelta(days=extension_days),
        )
        db.add(enrollment)

    db.commit()
    db.refresh(enrollment)
    return enrollment


def suspend_enrollment(
    db: Session, student_id: int, course_id: int
) -> Enrollment | None:
    enrollment = (
        db.query(Enrollment)
        .filter(Enrollment.student_id == student_id, Enrollment.course_id == course_id)
        .first()
    )
    if enrollment:
        enrollment.status = EnrollmentStatus.SUSPENDED
        db.commit()
        db.refresh(enrollment)
    return enrollment
