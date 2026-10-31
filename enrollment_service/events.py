from datetime import datetime, timezone

from shared import events as ev
from shared.messaging import publish


def publish_enrollment_activated(
    enrollment_id: int,
    student_id: int,
    course_id: int,
    expires_at: datetime | None,
    source: str = "payment",
):
    """A student was enrolled. ``source`` is "payment" or "manual" (an admin's grant earns no referral credit)."""
    publish(
        ev.ENROLLMENT_ACTIVATED,
        {
            "enrollment_id": enrollment_id,
            "student_id": student_id,
            "course_id": course_id,
            "source": source,
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )
