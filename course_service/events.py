"""Course events. Every change to the catalogue is announced, so other services keep their own copy."""

from models import Course, Module

from shared import events as ev
from shared.messaging import publish


def _value(x):
    return x.value if hasattr(x, "value") else x


def course_event(course: Course) -> dict:
    return {
        "id": course.id,
        "title": course.title,
        "grade": course.grade,
        "stream_ids": course.stream_ids or [],
        "course_type": _value(course.course_type),
        "status": _value(course.status),
        "price": float(course.price or 0),
        "instructor_id": course.instructor_id,
        "thumbnail_url": course.thumbnail_url,
    }


def module_event(module: Module) -> dict:
    return {
        "id": module.id,
        "course_id": module.course_id,
        "title": module.title,
        "position": module.position,
        "is_active": module.is_active,
    }


def announce_course(course: Course) -> None:
    publish(ev.COURSE_UPSERTED, course_event(course))


def announce_course_deleted(course_id: int) -> None:
    publish(ev.COURSE_DELETED, {"id": course_id})


def announce_module(module: Module) -> None:
    publish(ev.COURSE_MODULE_UPSERTED, module_event(module))


def announce_module_deleted(module_id: int) -> None:
    publish(ev.COURSE_MODULE_DELETED, {"id": module_id})


def publish_course_published(course_id: int, teacher_id: int, title: str) -> None:
    publish(
        ev.COURSE_PUBLISHED,
        {"course_id": course_id, "teacher_id": teacher_id, "title": title},
    )


def announce_everything(db) -> None:
    """Re-announce every course and lesson so a service with an empty copy catches up."""
    for course in db.query(Course).all():
        announce_course(course)
    for module in db.query(Module).all():
        announce_module(module)
