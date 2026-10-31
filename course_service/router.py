import csv
import io
import re

from access import is_enrolled
from database import get_db
from events import (
    announce_course,
    announce_course_deleted,
    announce_module,
    announce_module_deleted,
    publish_course_published,
)
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from models import (
    CheckpointQuestion,
    Course,
    CourseStatus,
    CourseType,
    Lesson,
    Material,
    Module,
    VideoCheckpoint,
)
from schemas import (
    CheckpointQuestionResponse,
    CheckpointQuestionUpdate,
    CheckpointResponse,
    CheckpointUpdate,
    CourseCreate,
    CourseResponse,
    CourseUpdate,
    CourseWithModulesResponse,
    LessonCreate,
    LessonResponse,
    LessonUpdate,
    MaterialCreate,
    MaterialResponse,
    MaterialUpdate,
    ModuleCreate,
    ModuleResponse,
    ModuleUpdate,
    PaginatedCoursesResponse,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

from shared.auth import get_optional_payload, require_role

router = APIRouter()

_ALL_ROLES = ["student", "teacher", "admin"]
_TEACHER_ADMIN = ["teacher", "admin"]


def get_allowed_grades_for_student(grade: int) -> list[int]:
    """
    Grade-level course visibility rules (matches the reference platform):
    - Grade 11 sees grades 10 & 11 (upper O/L unlocks lower)
    - Grade 13 sees grades 12 & 13 (upper A/L unlocks lower)
    - All other grades (5-10, 12) see only their own grade
    """
    if grade == 11:
        return [10, 11]
    if grade == 13:
        return [12, 13]
    return [grade]


# ---------------------------------------------------------------------------
# Ownership helper
# ---------------------------------------------------------------------------


def assert_course_owner(course: Course, requester_id: int, requester_role: str) -> None:
    if requester_role == "admin":
        return
    if course.instructor_id != requester_id:
        raise HTTPException(status_code=403, detail={"error": "NOT_COURSE_OWNER"})


def _get_course_or_404(course_id: int, db: Session) -> Course:
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail={"error": "COURSE_NOT_FOUND"})
    return course


def _get_module_or_404(module_id: int, course_id: int, db: Session) -> Module:
    module = (
        db.query(Module)
        .filter(Module.id == module_id, Module.course_id == course_id)
        .first()
    )
    if not module:
        raise HTTPException(status_code=404, detail={"error": "MODULE_NOT_FOUND"})
    return module


def _is_public_viewer(payload: dict | None) -> bool:
    """Anonymous visitors and students only see published/active content."""
    return not payload or payload.get("role", "").lower() == "student"


def _assert_viewable(course: Course, payload: dict | None) -> None:
    if _is_public_viewer(payload) and course.status != CourseStatus.PUBLISHED:
        raise HTTPException(status_code=404, detail={"error": "COURSE_NOT_FOUND"})


async def _assert_can_open_content(
    course: Course, payload: dict | None, module_id: int | None = None
) -> None:
    """Video ids and material files are paid content — students need an active enrollment."""
    if payload is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "AUTH_REQUIRED",
                "message": "Log in to access course content",
            },
        )
    if payload.get("role", "").lower() == "student" and not await is_enrolled(
        int(payload["sub"]), course.id, module_id
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "NOT_ENROLLED",
                "message": "Enroll in this course to access its content",
            },
        )


def _module_responses(
    course_ids: list[int], public_viewer: bool, db: Session
) -> dict[int, list[ModuleResponse]]:
    """Modules for many courses in two queries, keyed by course_id."""
    if not course_ids:
        return {}
    q = db.query(Module).filter(Module.course_id.in_(course_ids))
    if public_viewer:
        q = q.filter(Module.is_active.is_(True))
    modules = q.order_by(Module.position, Module.id).all()

    counts: dict[int, int] = {}
    if modules:
        counts = dict(
            db.query(Lesson.module_id, func.count(Lesson.id))
            .filter(Lesson.module_id.in_([m.id for m in modules]))
            .group_by(Lesson.module_id)
            .all()
        )

    grouped: dict[int, list[ModuleResponse]] = {cid: [] for cid in course_ids}
    for m in modules:
        grouped[m.course_id].append(
            ModuleResponse.from_orm_module(m, lesson_count=counts.get(m.id, 0))
        )
    return grouped


def _checkpoints_for(
    lesson_ids: list[int], db: Session
) -> dict[int, list[CheckpointResponse]]:
    """Checkpoints (with questions) for many videos in two queries, keyed by lesson id."""
    if not lesson_ids:
        return {}
    checkpoints = (
        db.query(VideoCheckpoint)
        .filter(VideoCheckpoint.lesson_id.in_(lesson_ids))
        .order_by(VideoCheckpoint.timestamp_seconds, VideoCheckpoint.id)
        .all()
    )
    questions: dict[int, list[CheckpointQuestionResponse]] = {}
    if checkpoints:
        for q in (
            db.query(CheckpointQuestion)
            .filter(CheckpointQuestion.checkpoint_id.in_([c.id for c in checkpoints]))
            .order_by(CheckpointQuestion.order_number, CheckpointQuestion.id)
        ):
            questions.setdefault(q.checkpoint_id, []).append(
                CheckpointQuestionResponse.model_validate(q)
            )
    grouped: dict[int, list[CheckpointResponse]] = {}
    for c in checkpoints:
        grouped.setdefault(c.lesson_id, []).append(
            CheckpointResponse(
                id=c.id,
                video_id=c.lesson_id,
                timestamp_seconds=c.timestamp_seconds,
                questions=questions.get(c.id, []),
            )
        )
    return grouped


def _delete_checkpoints(db: Session, condition) -> None:
    ids = [cid for (cid,) in db.query(VideoCheckpoint.id).filter(condition)]
    if ids:
        db.query(CheckpointQuestion).filter(
            CheckpointQuestion.checkpoint_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(VideoCheckpoint).filter(VideoCheckpoint.id.in_(ids)).delete(
            synchronize_session=False
        )


def _get_material_or_404(material_id: int, module_id: int, db: Session) -> Material:
    material = (
        db.query(Material)
        .filter(Material.id == material_id, Material.module_id == module_id)
        .first()
    )
    if not material:
        raise HTTPException(status_code=404, detail={"error": "MATERIAL_NOT_FOUND"})
    return material


def _assert_lesson_in_module(
    lesson_id: int | None, module_id: int, db: Session
) -> None:
    if lesson_id is None:
        return
    exists = (
        db.query(Lesson.id)
        .filter(Lesson.id == lesson_id, Lesson.module_id == module_id)
        .first()
    )
    if not exists:
        raise HTTPException(status_code=404, detail={"error": "LESSON_NOT_FOUND"})


# ---------------------------------------------------------------------------
# Course endpoints
# ---------------------------------------------------------------------------


@router.post("/", response_model=CourseResponse, status_code=201)
async def create_course(
    body: CourseCreate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    teacher_id = int(payload["sub"])
    course = Course(
        instructor_id=teacher_id,
        title=body.title,
        description=body.description,
        price=body.price,
        course_type=CourseType(body.course_type),
        grade=body.grade,
        stream_ids=body.stream_ids,
        thumbnail_url=body.thumbnail_url,
        thumbnail_public_id=body.thumbnail_public_id,
        status=CourseStatus.DRAFT,
    )
    db.add(course)
    db.commit()
    db.refresh(course)
    announce_course(course)
    return CourseResponse.from_orm_course(course)


@router.get("/", response_model=PaginatedCoursesResponse)
async def list_courses(
    status: str = Query(None),
    grade: int | None = Query(
        None,
        description="The viewer's own grade — applies the O/L (10↔11) and A/L (12↔13) unlock bands",
    ),
    stream_id: int | None = Query(
        None, description="The viewer's own A/L stream (only applied for grade 12/13)"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_modules: bool = Query(
        False, description="Nest each course's modules (lessons) in the response"
    ),
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    # Public browsing: anonymous visitors and students both only ever see
    # published courses; only a logged-in teacher/admin can filter by status.
    public_viewer = _is_public_viewer(payload)
    q = db.query(Course)

    apply_stream_filter = False
    if public_viewer:
        q = q.filter(Course.status == CourseStatus.PUBLISHED)
        if grade is not None:
            allowed = get_allowed_grades_for_student(grade)
            # A course with no grade set is legacy/ungated — stays visible to everyone.
            q = q.filter((Course.grade.in_(allowed)) | (Course.grade.is_(None)))
            # Streams only apply at A/L (12/13) — a course can belong to more
            # than one stream (e.g. Physics is shared by Physical Science and
            # Bio Science), so this is a JSON-array containment check done in
            # Python below rather than in SQL (cheap at this catalogue size,
            # and portable — the generic JSON column type has no efficient
            # cross-dialect "array contains" operator).
            apply_stream_filter = grade in (12, 13) and stream_id is not None
    elif status:
        try:
            q = q.filter(Course.status == CourseStatus(status))
        except ValueError:
            raise HTTPException(status_code=422, detail={"error": "INVALID_STATUS"})

    if apply_stream_filter:
        all_matching = q.order_by(Course.id).all()
        filtered = [
            c for c in all_matching if not c.stream_ids or stream_id in c.stream_ids
        ]
        total = len(filtered)
        courses = filtered[(page - 1) * page_size : (page - 1) * page_size + page_size]
    else:
        total = q.count()
        courses = (
            q.order_by(Course.id).offset((page - 1) * page_size).limit(page_size).all()
        )

    modules_by_course = (
        _module_responses([c.id for c in courses], public_viewer, db)
        if include_modules
        else {}
    )
    items = []
    for c in courses:
        item = CourseWithModulesResponse.from_orm_course(c)
        if include_modules:
            item.modules = modules_by_course.get(c.id, [])
        items.append(item)

    return PaginatedCoursesResponse(
        total=total, page=page, page_size=page_size, items=items
    )


@router.get("/{course_id}", response_model=CourseWithModulesResponse)
async def get_course(
    course_id: int,
    include_modules: bool = Query(False),
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    course = _get_course_or_404(course_id, db)
    _assert_viewable(course, payload)
    item = CourseWithModulesResponse.from_orm_course(course)
    if include_modules:
        item.modules = _module_responses(
            [course.id], _is_public_viewer(payload), db
        ).get(course.id, [])
    return item


@router.put("/{course_id}", response_model=CourseResponse)
async def update_course(
    course_id: int,
    body: CourseUpdate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)

    updates = body.model_dump(exclude_unset=True)

    publish_event = False
    if "status" in updates and updates["status"] == "PUBLISHED":
        effective_type = updates.get("course_type") or (
            course.course_type.value
            if hasattr(course.course_type, "value")
            else course.course_type
        )
        # Video courses need a lesson with at least one video; exam courses
        # keep their exams in assessment_service, so a lesson is enough here.
        content_q = db.query(Module).filter(Module.course_id == course_id)
        if effective_type == "video":
            content_q = content_q.join(Lesson, Lesson.module_id == Module.id)
        if not content_q.first():
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "PUBLISH_REQUIRES_CONTENT",
                    "message": (
                        "Add at least one lesson with a video before publishing"
                        if effective_type == "video"
                        else "Add at least one lesson before publishing"
                    ),
                },
            )
        publish_event = True

    for field, value in updates.items():
        if field == "status":
            course.status = CourseStatus(value)
            course.is_published = value == "PUBLISHED"
        elif field == "course_type":
            course.course_type = CourseType(value)
        else:
            setattr(course, field, value)

    db.commit()
    db.refresh(course)
    announce_course(course)

    if publish_event:
        publish_course_published(course.id, course.instructor_id, course.title)

    return CourseResponse.from_orm_course(course)


@router.delete("/{course_id}", status_code=204)
async def delete_course(
    course_id: int,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)
    # No FK constraints in this schema — clear children explicitly.
    _delete_checkpoints(db, VideoCheckpoint.course_id == course_id)
    db.query(Material).filter(Material.course_id == course_id).delete()
    db.query(Lesson).filter(Lesson.course_id == course_id).delete()
    module_ids = [
        m for (m,) in db.query(Module.id).filter(Module.course_id == course_id)
    ]
    db.query(Module).filter(Module.course_id == course_id).delete()
    db.delete(course)
    db.commit()
    for module_id in module_ids:
        announce_module_deleted(module_id)
    announce_course_deleted(course_id)


# ---------------------------------------------------------------------------
# Module endpoints
# ---------------------------------------------------------------------------


@router.post("/{course_id}/modules", response_model=ModuleResponse, status_code=201)
async def create_module(
    course_id: int,
    body: ModuleCreate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)

    module = Module(
        course_id=course_id,
        title=body.title,
        description=body.description,
        image_url=body.image_url,
        image_public_id=body.image_public_id,
        is_active=body.is_active,
        position=body.order,
    )
    db.add(module)
    db.commit()
    db.refresh(module)
    announce_module(module)
    return ModuleResponse.from_orm_module(module)


@router.get("/{course_id}/modules", response_model=list[ModuleResponse])
async def list_modules(
    course_id: int,
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    course = _get_course_or_404(course_id, db)
    _assert_viewable(course, payload)
    return _module_responses([course_id], _is_public_viewer(payload), db).get(
        course_id, []
    )


@router.put("/{course_id}/modules/{module_id}", response_model=ModuleResponse)
async def update_module(
    course_id: int,
    module_id: int,
    body: ModuleUpdate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)
    module = _get_module_or_404(module_id, course_id, db)

    updates = body.model_dump(exclude_unset=True)
    if "order" in updates:
        module.position = updates.pop("order")
    for field, value in updates.items():
        setattr(module, field, value)

    db.commit()
    db.refresh(module)
    announce_module(module)
    db.refresh(module)
    return ModuleResponse.from_orm_module(module)


@router.delete("/{course_id}/modules/{module_id}", status_code=204)
async def delete_module(
    course_id: int,
    module_id: int,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)
    module = _get_module_or_404(module_id, course_id, db)

    # No cross-table FK constraints in this schema — clear children first.
    lesson_ids = [
        lid for (lid,) in db.query(Lesson.id).filter(Lesson.module_id == module_id)
    ]
    if lesson_ids:
        _delete_checkpoints(db, VideoCheckpoint.lesson_id.in_(lesson_ids))
    db.query(Material).filter(Material.module_id == module_id).delete()
    db.query(Lesson).filter(Lesson.module_id == module_id).delete()
    db.delete(module)
    db.commit()
    announce_module_deleted(module_id)


# ---------------------------------------------------------------------------
# Lesson endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{course_id}/modules/{module_id}/lessons",
    response_model=LessonResponse,
    status_code=201,
)
async def create_lesson(
    course_id: int,
    module_id: int,
    body: LessonCreate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)
    _get_module_or_404(module_id, course_id, db)

    lesson = Lesson(
        module_id=module_id,
        course_id=course_id,
        title=body.title,
        description=body.description,
        youtube_video_id=body.youtube_video_id,
        cloudinary_asset_url=body.cloudinary_asset_url,
        cloudinary_public_id=body.cloudinary_public_id,
        duration_seconds=body.duration_seconds,
        position=body.order,
    )
    db.add(lesson)
    db.commit()
    db.refresh(lesson)
    return LessonResponse.from_orm_lesson(lesson)


@router.get(
    "/{course_id}/modules/{module_id}/lessons", response_model=list[LessonResponse]
)
async def list_lessons(
    course_id: int,
    module_id: int,
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    course = _get_course_or_404(course_id, db)
    module = _get_module_or_404(module_id, course_id, db)
    _assert_viewable(course, payload)
    if _is_public_viewer(payload) and not module.is_active:
        raise HTTPException(status_code=404, detail={"error": "MODULE_NOT_FOUND"})
    await _assert_can_open_content(course, payload, module_id)
    lessons = (
        db.query(Lesson)
        .filter(Lesson.module_id == module_id)
        .order_by(Lesson.position)
        .all()
    )
    by_lesson = _checkpoints_for([lesson.id for lesson in lessons], db)
    responses = []
    for lesson in lessons:
        item = LessonResponse.from_orm_lesson(lesson)
        item.checkpoints = by_lesson.get(lesson.id, [])
        responses.append(item)
    return responses


@router.put(
    "/{course_id}/modules/{module_id}/lessons/{lesson_id}",
    response_model=LessonResponse,
)
async def update_lesson(
    course_id: int,
    module_id: int,
    lesson_id: int,
    body: LessonUpdate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)
    _get_module_or_404(module_id, course_id, db)

    lesson = (
        db.query(Lesson)
        .filter(Lesson.id == lesson_id, Lesson.module_id == module_id)
        .first()
    )
    if not lesson:
        raise HTTPException(status_code=404, detail={"error": "LESSON_NOT_FOUND"})

    updates = body.model_dump(exclude_unset=True)
    if "order" in updates:
        lesson.position = updates.pop("order")
    for field, value in updates.items():
        setattr(lesson, field, value)

    db.commit()
    db.refresh(lesson)
    return LessonResponse.from_orm_lesson(lesson)


@router.delete("/{course_id}/modules/{module_id}/lessons/{lesson_id}", status_code=204)
async def delete_lesson(
    course_id: int,
    module_id: int,
    lesson_id: int,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)
    _get_module_or_404(module_id, course_id, db)

    lesson = (
        db.query(Lesson)
        .filter(Lesson.id == lesson_id, Lesson.module_id == module_id)
        .first()
    )
    if not lesson:
        raise HTTPException(status_code=404, detail={"error": "LESSON_NOT_FOUND"})

    _delete_checkpoints(db, VideoCheckpoint.lesson_id == lesson_id)
    # Materials pinned to this video stay in the module, just unpinned.
    db.query(Material).filter(Material.lesson_id == lesson_id).update(
        {Material.lesson_id: None}
    )
    db.delete(lesson)
    db.commit()


# ---------------------------------------------------------------------------
# Material endpoints (downloadable resources inside a module)
# ---------------------------------------------------------------------------


@router.get(
    "/{course_id}/modules/{module_id}/materials",
    response_model=list[MaterialResponse],
)
async def list_materials(
    course_id: int,
    module_id: int,
    lesson_id: int | None = Query(
        None, description="Only materials pinned to this video"
    ),
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    course = _get_course_or_404(course_id, db)
    module = _get_module_or_404(module_id, course_id, db)
    _assert_viewable(course, payload)
    if _is_public_viewer(payload) and not module.is_active:
        raise HTTPException(status_code=404, detail={"error": "MODULE_NOT_FOUND"})
    await _assert_can_open_content(course, payload, module_id)
    q = db.query(Material).filter(Material.module_id == module_id)
    if lesson_id is not None:
        q = q.filter(Material.lesson_id == lesson_id)
    materials = q.order_by(Material.position, Material.id).all()
    return [MaterialResponse.from_orm_material(m) for m in materials]


@router.post(
    "/{course_id}/modules/{module_id}/materials",
    response_model=MaterialResponse,
    status_code=201,
)
async def create_material(
    course_id: int,
    module_id: int,
    body: MaterialCreate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, int(payload["sub"]), payload.get("role", "").lower())
    _get_module_or_404(module_id, course_id, db)
    _assert_lesson_in_module(body.lesson_id, module_id, db)

    material = Material(
        course_id=course_id,
        module_id=module_id,
        lesson_id=body.lesson_id,
        title=body.title,
        file_url=body.file_url,
        file_type=body.file_type,
        file_public_id=body.file_public_id,
        position=body.order,
    )
    db.add(material)
    db.commit()
    db.refresh(material)
    return MaterialResponse.from_orm_material(material)


@router.put(
    "/{course_id}/modules/{module_id}/materials/{material_id}",
    response_model=MaterialResponse,
)
async def update_material(
    course_id: int,
    module_id: int,
    material_id: int,
    body: MaterialUpdate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, int(payload["sub"]), payload.get("role", "").lower())
    _get_module_or_404(module_id, course_id, db)
    material = _get_material_or_404(material_id, module_id, db)

    updates = body.model_dump(exclude_unset=True)
    if "lesson_id" in updates:
        _assert_lesson_in_module(updates["lesson_id"], module_id, db)
    if "order" in updates:
        material.position = updates.pop("order")
    for field, value in updates.items():
        setattr(material, field, value)

    db.commit()
    db.refresh(material)
    return MaterialResponse.from_orm_material(material)


@router.delete(
    "/{course_id}/modules/{module_id}/materials/{material_id}", status_code=204
)
async def delete_material(
    course_id: int,
    module_id: int,
    material_id: int,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, int(payload["sub"]), payload.get("role", "").lower())
    _get_module_or_404(module_id, course_id, db)
    material = _get_material_or_404(material_id, module_id, db)
    db.delete(material)
    db.commit()


# ---------------------------------------------------------------------------
# In-video quiz checkpoints (ungraded practice inside a video)
# ---------------------------------------------------------------------------

_CHECKPOINT_CSV_HEADER = (
    "timestamp_seconds,question_text,a,b,c,d,e,correct_option,explanation\n"
)
_CHECKPOINT_CSV_EXAMPLE = (
    "1:30,What is the first step of this process?,Gathering data,Analyzing data,Reporting,None of the above,,a,Always gather data first.\n"
    "10:00,Is this a blocking quiz?,Yes,No,,,,a,Students must answer correctly to proceed.\n"
)
_MAX_CHECKPOINT_CSV = 1024 * 1024


def _parse_timestamp(raw: str) -> int | None:
    """Seconds, MM:SS or HH:MM:SS."""
    raw = (raw or "").strip()
    if re.fullmatch(r"\d+", raw):
        return int(raw)
    parts = raw.split(":")
    if len(parts) in (2, 3) and all(re.fullmatch(r"\d+", part) for part in parts):
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + int(part)
        return seconds
    return None


def _get_video_for_owner(
    course_id: int, module_id: int, lesson_id: int, payload: dict, db: Session
) -> Lesson:
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, int(payload["sub"]), payload.get("role", "").lower())
    _get_module_or_404(module_id, course_id, db)
    lesson = (
        db.query(Lesson)
        .filter(Lesson.id == lesson_id, Lesson.module_id == module_id)
        .first()
    )
    if not lesson:
        raise HTTPException(status_code=404, detail={"error": "LESSON_NOT_FOUND"})
    return lesson


def _checkpoint_for_owner(
    checkpoint_id: int, payload: dict, db: Session
) -> VideoCheckpoint:
    cp = db.query(VideoCheckpoint).filter(VideoCheckpoint.id == checkpoint_id).first()
    if not cp:
        raise HTTPException(status_code=404, detail={"error": "CHECKPOINT_NOT_FOUND"})
    assert_course_owner(
        _get_course_or_404(cp.course_id, db),
        int(payload["sub"]),
        payload.get("role", "").lower(),
    )
    return cp


@router.get("/checkpoints/csv-template")
async def checkpoint_csv_template(_: dict = Depends(require_role(_TEACHER_ADMIN))):
    return Response(
        content=_CHECKPOINT_CSV_HEADER + _CHECKPOINT_CSV_EXAMPLE,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="video_checkpoints_template.csv"'
        },
    )


@router.get(
    "/{course_id}/modules/{module_id}/lessons/{lesson_id}/checkpoints",
    response_model=list[CheckpointResponse],
)
async def list_checkpoints(
    course_id: int,
    module_id: int,
    lesson_id: int,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    _get_video_for_owner(course_id, module_id, lesson_id, payload, db)
    return _checkpoints_for([lesson_id], db).get(lesson_id, [])


@router.post(
    "/{course_id}/modules/{module_id}/lessons/{lesson_id}/checkpoints/import-csv"
)
async def import_checkpoints_csv(
    course_id: int,
    module_id: int,
    lesson_id: int,
    file: UploadFile = File(...),
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    """Adds the CSV's questions; rows sharing a timestamp land on the same checkpoint (existing or new)."""
    _get_video_for_owner(course_id, module_id, lesson_id, payload, db)
    raw = await file.read()
    if len(raw) > _MAX_CHECKPOINT_CSV:
        raise HTTPException(
            status_code=413,
            detail={"error": "FILE_TOO_LARGE", "message": "CSV exceeds the 1 MB limit"},
        )
    try:
        text_content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "INVALID_ENCODING",
                "message": "CSV must be UTF-8 encoded",
            },
        )

    existing = {
        cp.timestamp_seconds: cp
        for cp in db.query(VideoCheckpoint).filter(
            VideoCheckpoint.lesson_id == lesson_id
        )
    }
    created_questions = 0
    touched: set[int] = set()
    errors: list[str] = []
    for row_number, row in enumerate(
        csv.DictReader(io.StringIO(text_content)), start=2
    ):

        def cell(name: str, row: dict = row) -> str:
            return (row.get(name) or "").strip()

        ts = _parse_timestamp(cell("timestamp_seconds"))
        if ts is None:
            errors.append(f"Row {row_number}: timestamp must be seconds or MM:SS")
            continue
        if not cell("question_text") or not cell("a") or not cell("b"):
            errors.append(f"Row {row_number}: question_text, a and b are required")
            continue
        filled = {letter for letter in "abcde" if cell(letter)}
        answer = cell("correct_option").lower()
        if answer not in filled:
            errors.append(
                f"Row {row_number}: correct_option must be one of {', '.join(sorted(filled))}"
            )
            continue

        cp = existing.get(ts)
        if cp is None:
            cp = VideoCheckpoint(
                course_id=course_id, lesson_id=lesson_id, timestamp_seconds=ts
            )
            db.add(cp)
            db.flush()
            existing[ts] = cp
        order = (
            db.query(CheckpointQuestion)
            .filter(CheckpointQuestion.checkpoint_id == cp.id)
            .count()
        )
        db.add(
            CheckpointQuestion(
                checkpoint_id=cp.id,
                question_text=cell("question_text"),
                option_a=cell("a"),
                option_b=cell("b"),
                option_c=cell("c") or None,
                option_d=cell("d") or None,
                option_e=cell("e") or None,
                correct_option=answer,
                explanation=cell("explanation") or None,
                order_number=order,
            )
        )
        db.flush()
        touched.add(cp.id)
        created_questions += 1

    db.commit()
    return {
        "message": f"Imported {created_questions} question(s) across {len(touched)} checkpoint(s)",
        "questions_created": created_questions,
        "errors": errors,
    }


@router.delete(
    "/{course_id}/modules/{module_id}/lessons/{lesson_id}/checkpoints", status_code=204
)
async def clear_checkpoints(
    course_id: int,
    module_id: int,
    lesson_id: int,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    _get_video_for_owner(course_id, module_id, lesson_id, payload, db)
    _delete_checkpoints(db, VideoCheckpoint.lesson_id == lesson_id)
    db.commit()


@router.patch("/checkpoints/{checkpoint_id}", response_model=CheckpointResponse)
async def update_checkpoint(
    checkpoint_id: int,
    body: CheckpointUpdate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    cp = _checkpoint_for_owner(checkpoint_id, payload, db)
    if body.timestamp_seconds < 0:
        raise HTTPException(status_code=422, detail={"error": "INVALID_TIMESTAMP"})
    cp.timestamp_seconds = body.timestamp_seconds
    db.commit()
    return next(
        c for c in _checkpoints_for([cp.lesson_id], db)[cp.lesson_id] if c.id == cp.id
    )


@router.delete("/checkpoints/{checkpoint_id}", status_code=204)
async def delete_checkpoint(
    checkpoint_id: int,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    cp = _checkpoint_for_owner(checkpoint_id, payload, db)
    _delete_checkpoints(db, VideoCheckpoint.id == cp.id)
    db.commit()


@router.patch(
    "/checkpoint-questions/{question_id}", response_model=CheckpointQuestionResponse
)
async def update_checkpoint_question(
    question_id: int,
    body: CheckpointQuestionUpdate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    q = (
        db.query(CheckpointQuestion)
        .filter(CheckpointQuestion.id == question_id)
        .first()
    )
    if not q:
        raise HTTPException(status_code=404, detail={"error": "QUESTION_NOT_FOUND"})
    _checkpoint_for_owner(q.checkpoint_id, payload, db)
    updates = body.model_dump(exclude_unset=True)
    for field in ("question_text", "option_a", "option_b"):
        if field in updates and not (updates[field] or "").strip():
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "FIELD_REQUIRED",
                    "message": f"{field} cannot be empty",
                },
            )
    for field, value in updates.items():
        setattr(
            q,
            field,
            (
                value.strip()
                if isinstance(value, str) and field != "correct_option"
                else value
            ),
        )
    answer = q.correct_option
    if not getattr(q, f"option_{answer}", None):
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "error": "INVALID_ANSWER",
                "message": "correct_option points at an empty option",
            },
        )
    db.commit()
    db.refresh(q)
    return q
