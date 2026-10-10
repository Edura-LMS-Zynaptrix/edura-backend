import secrets
import string

from auth_client import get_all_auth_users, get_auth_user
from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Query, status
from models import GradeSubject, ProfileRole, Stream, UserProfile
from schemas import (
    ReferralApplyRequest,
    GradeSubjectCreate,
    GradeSubjectResponse,
    GradeSubjectUpdate,
    InternalProfileCreateRequest,
    PaginatedUsersResponse,
    RoleStatsResponse,
    RoleUpdateRequest,
    StreamCreate,
    StreamResponse,
    StreamUpdate,
    UserProfileCreate,
    UserProfileResponse,
    UserProfileUpdate,
)
from referrals import apply_code as apply_referral_code
from referrals import bind_referral, find_referrer
from referrals import events_for as referral_events_for
from referrals import summary as referral_summary_of
from sqlalchemy.orm import Session

from shared.auth import require_role

router = APIRouter()

_ALL_ROLES = ["student", "teacher", "admin"]


def _to_response(
    profile: UserProfile, email: str | None = None, is_active: bool | None = None
) -> UserProfileResponse:
    return UserProfileResponse(
        id=profile.user_id,
        name=f"{profile.first_name} {profile.last_name}",
        email=email,
        role=profile.role.value,
        first_name=profile.first_name,
        last_name=profile.last_name,
        mobile_no=profile.mobile_no,
        date_of_birth=profile.date_of_birth,
        bio=profile.bio,
        avatar_url=profile.avatar_url,
        avatar_public_id=profile.avatar_public_id,
        school=profile.school,
        district=profile.district,
        grade=profile.grade,
        stream_id=profile.stream_id,
        nic_number=profile.nic_number,
        selected_subjects=profile.selected_subjects,
        referral_code=profile.referral_code,
        referred_by_user_id=profile.referred_by_user_id,
        is_active=is_active,
        created_at=profile.created_at,
    )


def _generate_referral_code(db: Session, first_name: str) -> str:
    """Short, human-shareable code like `JOHN4F2A` — collision-checked
    against the (unique) referral_code column with a handful of retries."""
    prefix = "".join(ch for ch in first_name.upper() if ch.isalpha())[:4] or "USER"
    for _ in range(10):
        suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
        code = f"{prefix}{suffix}"
        if not db.query(UserProfile).filter(UserProfile.referral_code == code).first():
            return code
    return secrets.token_hex(6).upper()


@router.get("/", response_model=PaginatedUsersResponse)
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    role: str | None = Query(None),
    search: str | None = Query(None),
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    """Admin: paginated list of all user profiles, enriched with the
    is_active/email fields that only auth_service's User row actually owns."""
    q = db.query(UserProfile)
    if role:
        try:
            q = q.filter(UserProfile.role == ProfileRole(role))
        except ValueError:
            raise HTTPException(status_code=422, detail={"error": "INVALID_ROLE"})
    if search:
        like = f"%{search}%"
        q = q.filter(
            (UserProfile.first_name.ilike(like)) | (UserProfile.last_name.ilike(like))
        )

    total = q.count()
    offset = (page - 1) * page_size
    profiles = q.order_by(UserProfile.id.desc()).offset(offset).limit(page_size).all()

    auth_users = await get_all_auth_users()
    items = []
    for p in profiles:
        auth_row = auth_users.get(p.user_id)
        items.append(
            _to_response(
                p,
                email=auth_row["email"] if auth_row else None,
                is_active=auth_row["is_active"] if auth_row else None,
            )
        )

    return PaginatedUsersResponse(total=total, page=page, page_size=page_size, items=items)


@router.post("/", response_model=UserProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_own_profile(
    body: UserProfileCreate,
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    """
    Create the profile for the currently authenticated user (self-service).
    auth_service creates the login record on /register but has no way to
    populate name/contact fields — the client calls this right after its
    first login to finish setting up the account. Role is taken from the
    JWT (assigned at registration), never from the request body.
    """
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()

    existing = db.query(UserProfile).filter(UserProfile.user_id == requester_id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "PROFILE_EXISTS", "message": "Profile already exists for this user"},
        )

    # An unrecognised code is ignored rather than blocking registration.
    referrer = find_referrer(db, body.referral_code)

    profile = UserProfile(
        user_id=requester_id,
        role=ProfileRole(requester_role),
        first_name=body.first_name,
        last_name=body.last_name,
        mobile_no=body.mobile_no,
        date_of_birth=body.date_of_birth,
        bio=body.bio,
        avatar_url=body.avatar_url,
        avatar_public_id=body.avatar_public_id,
        school=body.school,
        district=body.district,
        grade=body.grade,
        stream_id=body.stream_id,
        nic_number=body.nic_number,
        selected_subjects=body.selected_subjects,
        referral_code=_generate_referral_code(db, body.first_name),
    )
    db.add(profile)
    if referrer:
        bind_referral(db, profile, referrer)
    db.commit()
    db.refresh(profile)

    email = payload.get("email")
    return _to_response(profile, email=email)


@router.post("/internal/create-profile", response_model=UserProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_profile_internal(body: InternalProfileCreateRequest, db: Session = Depends(get_db)):
    """
    Unauthenticated, service-to-service only (blocked at the gateway). Called
    by auth_service when it creates an account itself: an admin made by
    another admin, or a first-time Google sign-in (seeded with the Google
    name and photo; the student adds academic details on complete-profile).
    """
    existing = db.query(UserProfile).filter(UserProfile.user_id == body.user_id).first()
    if existing:
        return _to_response(existing)

    try:
        role = ProfileRole(body.role)
    except ValueError:
        raise HTTPException(status_code=422, detail={"error": "INVALID_ROLE"})

    profile = UserProfile(
        user_id=body.user_id,
        role=role,
        first_name=body.first_name,
        last_name=body.last_name,
        avatar_url=body.avatar_url,
        referral_code=_generate_referral_code(db, body.first_name),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _to_response(profile)


# ── Streams & Subjects reference data ────────────────────────────────────────
# Registered before the /{user_id} catch-all below — otherwise FastAPI/Starlette
# matches "/streams" and "/subjects" against that path first and fails int()
# conversion on the segment instead of falling through to these routes.
# Public reads; writes are admin-only.


@router.get("/streams", response_model=list[StreamResponse])
async def list_streams(active_only: bool = Query(True), db: Session = Depends(get_db)):
    q = db.query(Stream)
    if active_only:
        q = q.filter(Stream.is_active == True)  # noqa: E712
    return q.order_by(Stream.name).all()


@router.post("/streams", response_model=StreamResponse, status_code=status.HTTP_201_CREATED)
async def create_stream(
    body: StreamCreate,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    stream = Stream(**body.model_dump())
    db.add(stream)
    db.commit()
    db.refresh(stream)
    return stream


@router.put("/streams/{stream_id}", response_model=StreamResponse)
async def update_stream(
    stream_id: int,
    body: StreamUpdate,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail={"error": "STREAM_NOT_FOUND"})
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(stream, field, value)
    db.commit()
    db.refresh(stream)
    return stream


@router.delete("/streams/{stream_id}", status_code=204)
async def delete_stream(
    stream_id: int,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail={"error": "STREAM_NOT_FOUND"})
    db.delete(stream)
    db.commit()


@router.get("/grade-subjects", response_model=list[GradeSubjectResponse])
async def list_grade_subjects(
    grade: int | None = Query(None),
    active_only: bool = Query(True),
    db: Session = Depends(get_db),
):
    q = db.query(GradeSubject)
    if grade is not None:
        q = q.filter(GradeSubject.grade == grade)
    if active_only:
        q = q.filter(GradeSubject.is_active == True)  # noqa: E712
    return q.order_by(GradeSubject.grade, GradeSubject.name).all()


@router.post("/grade-subjects", response_model=GradeSubjectResponse, status_code=status.HTTP_201_CREATED)
async def create_grade_subject(
    body: GradeSubjectCreate,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    subject = GradeSubject(**body.model_dump())
    db.add(subject)
    db.commit()
    db.refresh(subject)
    return subject


@router.put("/grade-subjects/{subject_id}", response_model=GradeSubjectResponse)
async def update_grade_subject(
    subject_id: int,
    body: GradeSubjectUpdate,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    subject = db.query(GradeSubject).filter(GradeSubject.id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=404, detail={"error": "SUBJECT_NOT_FOUND"})
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(subject, field, value)
    db.commit()
    db.refresh(subject)
    return subject


@router.delete("/grade-subjects/{subject_id}", status_code=204)
async def delete_grade_subject(
    subject_id: int,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    subject = db.query(GradeSubject).filter(GradeSubject.id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=404, detail={"error": "SUBJECT_NOT_FOUND"})
    db.delete(subject)
    db.commit()


@router.get("/subjects", response_model=list[str])
async def get_subjects_for_grade(
    grade: int = Query(..., ge=5, le=13),
    stream_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """
    Subjects available to pick from at registration/profile time.
    Grades 12-13 draw from the chosen stream's subject list; grades 5-11
    draw from the grade_subjects catalogue.
    """
    if grade in (12, 13):
        if stream_id is None:
            return []
        stream = db.query(Stream).filter(Stream.id == stream_id, Stream.is_active == True).first()  # noqa: E712
        return stream.subjects if stream else []

    rows = (
        db.query(GradeSubject)
        .filter(GradeSubject.grade == grade, GradeSubject.is_active == True)  # noqa: E712
        .order_by(GradeSubject.name)
        .all()
    )
    return [r.name for r in rows]


def _own_profile(payload: dict, db: Session) -> UserProfile:
    profile = db.query(UserProfile).filter(UserProfile.user_id == int(payload["sub"])).first()
    if not profile:
        raise HTTPException(status_code=404, detail={"error": "PROFILE_NOT_FOUND"})
    return profile


@router.get("/referral/summary")
async def referral_summary(
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    """The caller's code, share link and wallet totals."""
    return referral_summary_of(_own_profile(payload, db))


@router.get("/referral/events")
async def referral_events(
    limit: int = Query(50, ge=1, le=200),
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    """Students who signed up with the caller's code."""
    return referral_events_for(db, int(payload["sub"]), limit)


@router.post("/referral/apply")
async def referral_apply(
    body: ReferralApplyRequest,
    payload: dict = Depends(require_role(["student"])),
    db: Session = Depends(get_db),
):
    return apply_referral_code(db, _own_profile(payload, db), body.referral_code)


@router.get("/internal/students")
async def students_internal(
    grades: str | None = Query(None, description="Comma-separated grades; omit for all"),
    search: str | None = Query(None, max_length=100),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Service-to-service only (blocked at the gateway): student picker for admin access grants."""
    q = db.query(UserProfile).filter(UserProfile.role == ProfileRole.student)
    if grades:
        try:
            wanted = [int(g) for g in grades.split(",") if g.strip()]
        except ValueError:
            raise HTTPException(status_code=422, detail={"error": "INVALID_GRADES"})
        q = q.filter(UserProfile.grade.in_(wanted))
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(
            (UserProfile.first_name.ilike(term))
            | (UserProfile.last_name.ilike(term))
            | (UserProfile.school.ilike(term))
        )
    return [
        {
            "user_id": str(p.user_id),
            "full_name": f"{p.first_name} {p.last_name}".strip(),
            "grade": p.grade,
            "district": p.district,
            "school": p.school,
        }
        for p in q.order_by(UserProfile.first_name, UserProfile.last_name).limit(limit).all()
    ]


@router.get("/internal/profiles/{user_id}")
def profile_internal(user_id: int, db: Session = Depends(get_db)):
    """Service-to-service only (blocked at the gateway): a profile for reports."""
    p = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not p:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})
    return {
        "user_id": p.user_id,
        "role": p.role.value,
        "full_name": f"{p.first_name} {p.last_name}".strip(),
        "grade": p.grade,
        "school": p.school,
        "district": p.district,
        "mobile_no": p.mobile_no,
    }


@router.get("/internal/profiles")
def profiles_internal(
    ids: str = Query(..., description="Comma-separated auth user ids"),
    db: Session = Depends(get_db),
):
    """Service-to-service only (blocked at the gateway): leaderboard details for many users."""
    try:
        wanted = [int(x) for x in ids.split(",") if x.strip()][:500]
    except ValueError:
        raise HTTPException(status_code=422, detail={"error": "INVALID_IDS"})
    return [
        {
            "user_id": p.user_id,
            "role": p.role.value,
            "full_name": f"{p.first_name} {p.last_name}".strip(),
            "school": p.school,
            "district": p.district,
            "grade": p.grade,
        }
        for p in db.query(UserProfile).filter(UserProfile.user_id.in_(wanted))
    ]


@router.get("/internal/names")
async def profile_names_internal(
    ids: str = Query(..., description="Comma-separated auth user ids"),
    db: Session = Depends(get_db),
):
    """Service-to-service only (blocked at the gateway): {user_id: display name}."""
    try:
        wanted = [int(x) for x in ids.split(",") if x.strip()][:500]
    except ValueError:
        raise HTTPException(status_code=422, detail={"error": "INVALID_IDS"})
    rows = (
        db.query(UserProfile.user_id, UserProfile.first_name, UserProfile.last_name)
        .filter(UserProfile.user_id.in_(wanted))
        .all()
    )
    return {str(uid): f"{first} {last}".strip() for uid, first, last in rows}


@router.get("/internal/stats", response_model=RoleStatsResponse)
async def get_role_stats_internal(db: Session = Depends(get_db)):
    """Service-to-service only (blocked at the gateway): analytics_service's public homepage stats."""
    return RoleStatsResponse(
        total_students=db.query(UserProfile).filter(UserProfile.role == ProfileRole.student).count(),
        total_teachers=db.query(UserProfile).filter(UserProfile.role == ProfileRole.teacher).count(),
        total_admins=db.query(UserProfile).filter(UserProfile.role == ProfileRole.admin).count(),
    )


@router.get("/stats", response_model=RoleStatsResponse)
async def get_role_stats(
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    """Admin dashboard headline counts — cheap grouped COUNT query, no
    cross-service call needed since role lives on the profile itself."""
    return RoleStatsResponse(
        total_students=db.query(UserProfile).filter(UserProfile.role == ProfileRole.student).count(),
        total_teachers=db.query(UserProfile).filter(UserProfile.role == ProfileRole.teacher).count(),
        total_admins=db.query(UserProfile).filter(UserProfile.role == ProfileRole.admin).count(),
    )


@router.get("/{user_id}", response_model=UserProfileResponse)
async def get_user(
    user_id: int,
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    """Get a user profile. Own record always allowed; other records require admin."""
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()

    if requester_id != user_id and requester_role != "admin":
        raise HTTPException(
            status_code=403,
            detail={"error": "INSUFFICIENT_PERMISSIONS"},
        )

    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})

    # Include email from JWT when the requester is viewing their own record;
    # for any other record (an admin viewing someone else's), ask
    # auth_service for both email and is_active.
    if requester_id == user_id:
        email = payload.get("email")
        is_active = None
    else:
        auth_row = await get_auth_user(user_id)
        email = auth_row["email"] if auth_row else None
        is_active = auth_row["is_active"] if auth_row else None
    return _to_response(profile, email=email, is_active=is_active)


@router.put("/{user_id}", response_model=UserProfileResponse)
async def update_user(
    user_id: int,
    body: UserProfileUpdate,
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    """Update a user profile. Own record allowed; other records require admin."""
    requester_id = int(payload["sub"])
    requester_role = payload.get("role", "").lower()

    if requester_id != user_id and requester_role != "admin":
        raise HTTPException(
            status_code=403,
            detail={"error": "INSUFFICIENT_PERMISSIONS"},
        )

    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})

    updates = body.model_dump(exclude_unset=True)
    # An unrecognised code (or the user's own) is ignored, as at registration.
    referrer = find_referrer(db, updates.pop("referral_code", None))
    if referrer:
        bind_referral(db, profile, referrer)

    for field, value in updates.items():
        setattr(profile, field, value)

    db.commit()
    db.refresh(profile)
    return _to_response(profile)


@router.put("/{user_id}/role", response_model=UserProfileResponse)
async def update_user_role(
    user_id: int,
    body: RoleUpdateRequest,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    """Admin only: assign a new role to a user."""
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})

    profile.role = ProfileRole(body.role)
    db.commit()
    db.refresh(profile)
    return _to_response(profile)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    """Admin only: soft-delete a user profile. Auth service is notified via RabbitMQ (integration epic)."""
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})

    db.delete(profile)
    db.commit()
