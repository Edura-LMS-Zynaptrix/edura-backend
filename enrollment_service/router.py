from datetime import datetime, timezone

from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from models import ClassEnrollment, ClassPackage, Enrollment, EnrollmentStatus, LessonAccess
from package_access import active_packages, course_access, grant_package, has_access, lesson_grants, package_covers
from schemas import (
    AccessEnrolledStudent,
    AccessGrantRequest,
    AccessStudent,
    ClassEnrollmentGrant,
    ClassPackageCreate,
    ClassPackageUpdate,
    EnrollmentResponse,
)
from service_clients import allowed_student_grades, course_info, names, published_courses, streams, students
from sqlalchemy import func
from sqlalchemy.orm import Session

from shared.auth import get_optional_payload, require_role

router = APIRouter()


@router.get("/internal/course-counts")
def course_enrollment_counts_internal(db: Session = Depends(get_db)):
    """Service-to-service only (not routed by the gateway): active enrollments per course."""
    rows = (
        db.query(Enrollment.course_id, func.count(Enrollment.id))
        .filter(Enrollment.status == EnrollmentStatus.ACTIVE)
        .group_by(Enrollment.course_id)
        .all()
    )
    return [{"course_id": course_id, "count": count} for course_id, count in rows]


@router.get("/api/enrollments/me", response_model=list[EnrollmentResponse])
def list_my_enrollments(
    course_id: int | None = Query(None, description="Only this course"),
    payload: dict = Depends(require_role(["student", "teacher", "admin"])),
    db: Session = Depends(get_db),
):
    """The caller's own enrollments — identity comes from the JWT, never a query param."""
    student_id = int(payload["sub"])
    q = db.query(Enrollment).filter(Enrollment.student_id == student_id)
    grants = db.query(LessonAccess).filter(LessonAccess.student_id == student_id)
    if course_id is not None:
        q = q.filter(Enrollment.course_id == course_id)
        grants = grants.filter(LessonAccess.course_id == course_id)
    return [
        EnrollmentResponse.from_orm_model(e)
        for e in q.order_by(Enrollment.enrolled_at.desc()).all()
    ] + [EnrollmentResponse.from_lesson_access(g) for g in grants.all()]


def _stored_access(db: Session, student_id: int):
    courses = {
        cid for (cid,) in db.query(Enrollment.course_id).filter(
            Enrollment.student_id == student_id, Enrollment.status == EnrollmentStatus.ACTIVE
        )
    }
    grants = db.query(LessonAccess.course_id, LessonAccess.module_id).filter(
        LessonAccess.student_id == student_id
    ).all()
    return courses, active_packages(db, student_id), grants


@router.get("/internal/students/{student_id}/accessible")
async def accessible_internal(student_id: int, db: Session = Depends(get_db)):
    """Service-to-service only: whole courses the student can open, plus lessons granted on their own."""
    # The queries run off the event loop so concurrent callers aren't serialized behind them.
    courses, packages, grants = await run_in_threadpool(_stored_access, db, student_id)
    if packages:
        for course in await published_courses():
            if any(package_covers(p.grade, p.stream_id, course) for p in packages):
                courses.add(course["id"])
    lessons = [module_id for (course_id, module_id) in grants if course_id not in courses]
    return {"course_ids": sorted(courses), "lesson_ids": sorted(lessons)}


@router.get("/internal/access")
async def access_check_internal(
    student_id: int = Query(...),
    course_id: int = Query(...),
    module_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Service-to-service only (not routed by the gateway): may this student open this course / lesson?"""
    return {"has_access": await has_access(db, student_id, course_id, module_id)}


@router.get("/api/enrollments/me/access")
async def my_course_access(
    course_id: int = Query(...),
    payload: dict = Depends(require_role(["student", "teacher", "admin"])),
    db: Session = Depends(get_db),
):
    """What the caller can open in one course: everything, or just some lessons."""
    student_id = int(payload["sub"])
    whole_course, via = await course_access(db, student_id, course_id)
    return {
        "course": whole_course,
        "via": via,
        "lessons": [] if whole_course else lesson_grants(db, student_id, course_id),
    }


# ---------------------------------------------------------------------------
# Class packages — grade bundles that unlock every course they cover
# ---------------------------------------------------------------------------


def _serialize_package(pkg: ClassPackage, stream_names: dict[int, str], enrolled: set[int] | None = None) -> dict:
    data = {
        "id": str(pkg.id),
        "name": pkg.name,
        "description": pkg.description,
        "grade": pkg.grade,
        "stream_id": str(pkg.stream_id) if pkg.stream_id is not None else None,
        "stream_name": stream_names.get(pkg.stream_id) if pkg.stream_id is not None else None,
        "price": float(pkg.price or 0),
        "is_active": pkg.is_active,
        "created_at": pkg.created_at,
        "updated_at": pkg.updated_at,
    }
    if enrolled is not None:
        data["is_enrolled"] = pkg.id in enrolled
    return data


def _active_package_ids(db: Session, user_id: int) -> set[int]:
    return {
        pid
        for (pid,) in db.query(ClassEnrollment.class_package_id).filter(
            ClassEnrollment.user_id == user_id, ClassEnrollment.status == "active"
        )
    }


@router.get("/api/enrollments/class-packages")
async def list_class_packages(
    grade: int | None = Query(None, description="The viewer's own grade"),
    stream_id: int | None = Query(None, description="The viewer's own A/L stream"),
    payload: dict | None = Depends(get_optional_payload),
    db: Session = Depends(get_db),
):
    """Active packages; with a grade, only that grade's (grade 11 and 13 packages already cover the lower year)."""
    q = db.query(ClassPackage).filter(ClassPackage.is_active.is_(True))
    if grade is not None:
        q = q.filter(ClassPackage.grade == grade)
        if grade in (12, 13) and stream_id is not None:
            q = q.filter((ClassPackage.stream_id == stream_id) | (ClassPackage.stream_id.is_(None)))
    packages = q.order_by(ClassPackage.grade, ClassPackage.name).all()
    enrolled = _active_package_ids(db, int(payload["sub"])) if payload else set()
    names_by_stream = await streams()
    return [_serialize_package(p, names_by_stream, enrolled) for p in packages]


@router.post("/api/enrollments/class-packages/{package_id}/enroll-free")
def enroll_free_class_package(
    package_id: int,
    payload: dict = Depends(require_role(["student"])),
    db: Session = Depends(get_db),
):
    pkg = db.query(ClassPackage).filter(ClassPackage.id == package_id, ClassPackage.is_active.is_(True)).first()
    if pkg is None:
        raise HTTPException(status_code=404, detail={"error": "PACKAGE_NOT_FOUND"})
    if float(pkg.price or 0) != 0:
        raise HTTPException(status_code=400, detail={"error": "PAYMENT_REQUIRED", "message": "This class package requires payment"})
    user_id = int(payload["sub"])
    if package_id in _active_package_ids(db, user_id):
        raise HTTPException(status_code=400, detail={"error": "ALREADY_ENROLLED", "message": "Already enrolled in this class package"})
    enrollment = grant_package(db, user_id, package_id)
    db.commit()
    db.refresh(enrollment)
    return {
        "id": str(enrollment.id),
        "class_package_id": str(enrollment.class_package_id),
        "status": enrollment.status,
        "enrolled_at": enrollment.enrolled_at,
        "expires_at": enrollment.expires_at,
    }


@router.get("/api/enrollments/my-class-enrollments")
async def my_class_enrollments(
    payload: dict = Depends(require_role(["student", "teacher", "admin"])),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(ClassEnrollment, ClassPackage)
        .join(ClassPackage, ClassPackage.id == ClassEnrollment.class_package_id)
        .filter(ClassEnrollment.user_id == int(payload["sub"]), ClassEnrollment.status == "active")
        .order_by(ClassEnrollment.enrolled_at.desc())
        .all()
    )
    names_by_stream = await streams()
    return [
        {
            "id": str(e.id),
            "class_package_id": str(p.id),
            "package_name": p.name,
            "package_grade": p.grade,
            "package_stream_name": names_by_stream.get(p.stream_id) if p.stream_id is not None else None,
            "status": e.status,
            "enrolled_at": e.enrolled_at,
            "expires_at": e.expires_at,
        }
        for e, p in rows
    ]


def _get_package_or_404(package_id: int, db: Session) -> ClassPackage:
    pkg = db.query(ClassPackage).filter(ClassPackage.id == package_id).first()
    if pkg is None:
        raise HTTPException(status_code=404, detail={"error": "PACKAGE_NOT_FOUND"})
    return pkg


@router.get("/api/enrollments/admin/class-packages")
async def admin_list_class_packages(
    grade: int | None = Query(None),
    stream_id: int | None = Query(None),
    is_active: bool | None = Query(None),
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    q = db.query(ClassPackage)
    if grade is not None:
        q = q.filter(ClassPackage.grade == grade)
    if stream_id is not None:
        q = q.filter(ClassPackage.stream_id == stream_id)
    if is_active is not None:
        q = q.filter(ClassPackage.is_active.is_(is_active))
    names_by_stream = await streams()
    return [_serialize_package(p, names_by_stream) for p in q.order_by(ClassPackage.grade, ClassPackage.name).all()]


@router.post("/api/enrollments/admin/class-packages", status_code=201)
async def admin_create_class_package(
    body: ClassPackageCreate,
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    pkg = ClassPackage(**body.model_dump())
    db.add(pkg)
    db.commit()
    db.refresh(pkg)
    return _serialize_package(pkg, await streams())


# Static path: must stay above the /{package_id} routes.
@router.post("/api/enrollments/admin/class-packages/auto-generate")
async def admin_auto_generate_class_packages(
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    """Create the missing free packages: one per grade 5-11, and one per active stream for 12 and 13."""
    created: list[str] = []
    for grade in range(5, 12):
        if not db.query(ClassPackage.id).filter(ClassPackage.grade == grade, ClassPackage.stream_id.is_(None)).first():
            label = "Grade 10 & 11 courses" if grade == 11 else f"Grade {grade} courses"
            db.add(ClassPackage(name=f"Grade {grade} Class", description=f"Access to all {label}",
                                grade=grade, stream_id=None, price=0, is_active=True))
            created.append(f"Grade {grade}")
    for stream_id, stream_name in (await streams()).items():
        for grade in (12, 13):
            if not db.query(ClassPackage.id).filter(ClassPackage.grade == grade, ClassPackage.stream_id == stream_id).first():
                years = "Grade 12 & 13" if grade == 13 else "Grade 12"
                db.add(ClassPackage(name=f"Grade {grade} - {stream_name}",
                                    description=f"Access to all {years} {stream_name} courses",
                                    grade=grade, stream_id=stream_id, price=0, is_active=True))
                created.append(f"Grade {grade} {stream_name}")
    db.commit()
    return {"created": created, "count": len(created)}


@router.patch("/api/enrollments/admin/class-packages/{package_id}")
async def admin_update_class_package(
    package_id: int,
    body: ClassPackageUpdate,
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    pkg = _get_package_or_404(package_id, db)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(pkg, field, value)
    db.commit()
    db.refresh(pkg)
    return _serialize_package(pkg, await streams())


@router.delete("/api/enrollments/admin/class-packages/{package_id}")
def admin_delete_class_package(
    package_id: int,
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    pkg = _get_package_or_404(package_id, db)
    db.query(ClassEnrollment).filter(ClassEnrollment.class_package_id == pkg.id).delete()
    db.delete(pkg)
    db.commit()
    return {"status": "success"}


@router.get("/api/enrollments/admin/class-enrollments")
async def admin_list_class_enrollments(
    user_id: int | None = Query(None),
    class_package_id: int | None = Query(None),
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    q = db.query(ClassEnrollment, ClassPackage).join(ClassPackage, ClassPackage.id == ClassEnrollment.class_package_id)
    if user_id is not None:
        q = q.filter(ClassEnrollment.user_id == user_id)
    if class_package_id is not None:
        q = q.filter(ClassEnrollment.class_package_id == class_package_id)
    rows = q.order_by(ClassEnrollment.enrolled_at.desc()).all()
    display = await names([e.user_id for e, _ in rows])
    return [
        {
            "id": str(e.id),
            "user_id": str(e.user_id),
            "user_email": display.get(e.user_id),
            "class_package_id": str(p.id),
            "package_name": p.name,
            "package_grade": p.grade,
            "payment_id": str(e.payment_id) if e.payment_id is not None else None,
            "status": e.status,
            "enrolled_at": e.enrolled_at,
            "expires_at": e.expires_at,
        }
        for e, p in rows
    ]


@router.post("/api/enrollments/admin/class-enrollments", status_code=201)
def admin_grant_class_enrollment(
    body: ClassEnrollmentGrant,
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    _get_package_or_404(body.class_package_id, db)
    enrollment = grant_package(db, body.user_id, body.class_package_id, expires_at=body.expires_at)
    db.commit()
    db.refresh(enrollment)
    return {
        "id": str(enrollment.id),
        "user_id": str(enrollment.user_id),
        "class_package_id": str(enrollment.class_package_id),
        "status": enrollment.status,
        "enrolled_at": enrollment.enrolled_at,
        "expires_at": enrollment.expires_at,
    }


@router.delete("/api/enrollments/admin/class-enrollments/{enrollment_id}")
def admin_revoke_class_enrollment(
    enrollment_id: int,
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    deleted = db.query(ClassEnrollment).filter(ClassEnrollment.id == enrollment_id).delete()
    if not deleted:
        raise HTTPException(status_code=404, detail={"error": "CLASS_ENROLLMENT_NOT_FOUND"})
    db.commit()
    return {"status": "success"}


# ---------------------------------------------------------------------------
# Admin: manual access (whole course, or one lesson)
# ---------------------------------------------------------------------------

_ADMIN = ["admin"]


def _granted_ids(db: Session, course_id: int, module_id: int | None) -> dict[int, tuple[datetime, bool]]:
    """user_id -> (since, is_manual) for everyone who already has this access."""
    rows: dict[int, tuple[datetime, bool]] = {}
    for e in db.query(Enrollment).filter(
        Enrollment.course_id == course_id, Enrollment.status == EnrollmentStatus.ACTIVE
    ):
        rows[e.student_id] = (e.enrolled_at, bool(e.is_manual))
    if module_id is not None:
        for g in db.query(LessonAccess).filter(LessonAccess.module_id == module_id):
            rows.setdefault(g.student_id, (g.created_at, True))
    return rows


@router.get("/api/enrollments/admin/enrolled", response_model=list[AccessEnrolledStudent])
async def admin_enrolled_students(
    course_id: int = Query(...),
    module_id: int | None = Query(None),
    _: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    granted = _granted_ids(db, course_id, module_id)
    display = await names(list(granted))
    return sorted(
        (
            AccessEnrolledStudent(
                user_id=str(uid), full_name=display.get(uid, f"User #{uid}"), enrolled_at=since, is_manual=manual
            )
            for uid, (since, manual) in granted.items()
        ),
        key=lambda s: s.enrolled_at,
        reverse=True,
    )


@router.get("/api/enrollments/admin/eligible", response_model=list[AccessStudent])
async def admin_eligible_students(
    course_id: int = Query(...),
    module_id: int | None = Query(None),
    search: str | None = Query(None, max_length=100),
    _: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    """Students in the course's grade band who don't have this access yet."""
    course = await course_info(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail={"error": "COURSE_NOT_FOUND"})
    granted = _granted_ids(db, course_id, module_id)
    candidates = await students(allowed_student_grades(course.get("grade")), search)
    return [AccessStudent(**c) for c in candidates if int(c["user_id"]) not in granted]


@router.post("/api/enrollments/admin/grant")
async def admin_grant_access(
    body: AccessGrantRequest,
    payload: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    if await course_info(body.course_id) is None:
        raise HTTPException(status_code=404, detail={"error": "COURSE_NOT_FOUND"})
    admin_id = int(payload["sub"])
    now = datetime.now(timezone.utc)
    granted = 0
    for user_id in set(body.user_ids):
        if body.module_id is None:
            enrollment = (
                db.query(Enrollment)
                .filter(Enrollment.student_id == user_id, Enrollment.course_id == body.course_id)
                .first()
            )
            if enrollment is None:
                db.add(Enrollment(
                    student_id=user_id, course_id=body.course_id, status=EnrollmentStatus.ACTIVE,
                    enrolled_at=now, expires_at=None, is_manual=True,
                ))
                granted += 1
            elif enrollment.status != EnrollmentStatus.ACTIVE:
                enrollment.status = EnrollmentStatus.ACTIVE
                enrollment.enrolled_at = now
                enrollment.expires_at = None
                enrollment.is_manual = True
                granted += 1
        elif not db.query(LessonAccess.id).filter(
            LessonAccess.student_id == user_id, LessonAccess.module_id == body.module_id
        ).first():
            db.add(LessonAccess(
                student_id=user_id, course_id=body.course_id, module_id=body.module_id, granted_by=admin_id,
            ))
            granted += 1
    db.commit()
    return {"message": f"Access granted to {granted} student(s)", "granted": granted}


@router.delete("/api/enrollments/admin/revoke", status_code=status.HTTP_200_OK)
def admin_revoke_access(
    course_id: int = Query(...),
    user_id: int = Query(...),
    module_id: int | None = Query(None),
    _: dict = Depends(require_role(_ADMIN)),
    db: Session = Depends(get_db),
):
    if module_id is None:
        enrollment = (
            db.query(Enrollment)
            .filter(Enrollment.student_id == user_id, Enrollment.course_id == course_id)
            .first()
        )
        if enrollment is None or enrollment.status != EnrollmentStatus.ACTIVE:
            raise HTTPException(status_code=404, detail={"error": "ENROLLMENT_NOT_FOUND"})
        # Kept as a record (payments may reference it), just no longer active.
        enrollment.status = EnrollmentStatus.CANCELLED
    else:
        removed = db.query(LessonAccess).filter(
            LessonAccess.student_id == user_id, LessonAccess.module_id == module_id
        ).delete()
        if not removed:
            raise HTTPException(status_code=404, detail={"error": "ACCESS_NOT_FOUND"})
    db.commit()
    return {"message": "Access revoked"}


@router.get(
    "/api/enrollments",
    response_model=EnrollmentResponse,
    status_code=status.HTTP_200_OK,
)
@router.get(
    "/enrollments",
    response_model=EnrollmentResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
def get_enrollment(
    student_id: int = Query(..., description="ID of the student"),
    course_id: int = Query(..., description="ID of the course"),
    db: Session = Depends(get_db),
):
    """
    Query enrollment status by student_id and course_id.
    Exposed so other services (e.g., content_service) can verify student access.
    """
    enrollment = (
        db.query(Enrollment)
        .filter(
            Enrollment.student_id == student_id,
            Enrollment.course_id == course_id,
        )
        .first()
    )
    if not enrollment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Enrollment record not found for student and course",
        )
    return EnrollmentResponse.from_orm_model(enrollment)
