from database import get_db
from events import publish_course_published
from fastapi import APIRouter, Depends, HTTPException, Query
from models import Course, CourseStatus, Lesson, Module
from schemas import (
    CourseCreate,
    CourseResponse,
    CourseUpdate,
    LessonCreate,
    LessonResponse,
    ModuleCreate,
    ModuleResponse,
    PaginatedCoursesResponse,
)
from sqlalchemy.orm import Session

from shared.auth import require_role

router = APIRouter()

_ALL_ROLES = ["student", "teacher", "admin"]
_TEACHER_ADMIN = ["teacher", "admin"]


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
        status=CourseStatus.DRAFT,
    )
    db.add(course)
    db.commit()
    db.refresh(course)
    return CourseResponse.from_orm_course(course)


@router.get("/", response_model=PaginatedCoursesResponse)
async def list_courses(
    status: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    requester_role = payload.get("role", "")
    q = db.query(Course)

    if requester_role == "student":
        q = q.filter(Course.status == CourseStatus.PUBLISHED)
    elif status:
        try:
            q = q.filter(Course.status == CourseStatus(status))
        except ValueError:
            raise HTTPException(status_code=422, detail={"error": "INVALID_STATUS"})

    total = q.count()
    courses = q.offset((page - 1) * page_size).limit(page_size).all()
    return PaginatedCoursesResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[CourseResponse.from_orm_course(c) for c in courses],
    )


@router.get("/{course_id}", response_model=CourseResponse)
async def get_course(
    course_id: int,
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    course = _get_course_or_404(course_id, db)
    requester_role = payload.get("role", "")
    if requester_role == "student" and course.status != CourseStatus.PUBLISHED:
        raise HTTPException(status_code=404, detail={"error": "COURSE_NOT_FOUND"})
    return CourseResponse.from_orm_course(course)


@router.put("/{course_id}", response_model=CourseResponse)
async def update_course(
    course_id: int,
    body: CourseUpdate,
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
    db: Session = Depends(get_db),
):
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "")
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)

    updates = body.model_dump(exclude_unset=True)

    publish_event = False
    if "status" in updates and updates["status"] == "PUBLISHED":
        module_with_lesson = (
            db.query(Module)
            .filter(Module.course_id == course_id)
            .join(Lesson, Lesson.module_id == Module.id)
            .first()
        )
        if not module_with_lesson:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "PUBLISH_REQUIRES_CONTENT",
                    "message": "Course must have at least one module with one lesson before publishing",
                },
            )
        publish_event = True

    for field, value in updates.items():
        if field == "status":
            course.status = CourseStatus(value)
            course.is_published = value == "PUBLISHED"
        else:
            setattr(course, field, value)

    db.commit()
    db.refresh(course)

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
    requester_role = payload.get("role", "")
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)
    db.delete(course)
    db.commit()


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
    requester_role = payload.get("role", "")
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)

    module = Module(course_id=course_id, title=body.title, position=body.order)
    db.add(module)
    db.commit()
    db.refresh(module)
    return ModuleResponse.from_orm_module(module)


@router.get("/{course_id}/modules", response_model=list[ModuleResponse])
async def list_modules(
    course_id: int,
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    _get_course_or_404(course_id, db)
    modules = (
        db.query(Module)
        .filter(Module.course_id == course_id)
        .order_by(Module.position)
        .all()
    )
    return [ModuleResponse.from_orm_module(m) for m in modules]


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
    requester_role = payload.get("role", "")
    course = _get_course_or_404(course_id, db)
    assert_course_owner(course, requester_id, requester_role)
    _get_module_or_404(module_id, course_id, db)

    lesson = Lesson(
        module_id=module_id,
        course_id=course_id,
        title=body.title,
        youtube_video_id=body.youtube_video_id,
        cloudinary_asset_url=body.cloudinary_asset_url,
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
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    _get_course_or_404(course_id, db)
    _get_module_or_404(module_id, course_id, db)
    lessons = (
        db.query(Lesson)
        .filter(Lesson.module_id == module_id)
        .order_by(Lesson.position)
        .all()
    )
    return [LessonResponse.from_orm_lesson(lesson) for lesson in lessons]
