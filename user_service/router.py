from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Query, status
from models import GradeSubject, ProfileRole, ReplicaAccount, Stream, UserProfile
from profile_service import (
    announce_profile,
    announce_profile_deleted,
    announce_stream,
    create_profile,
)
from referrals import apply_code as apply_referral_code
from referrals import bind_referral, find_referrer
from referrals import events_for as referral_events_for
from referrals import summary as referral_summary_of
from schemas import (
    GradeSubjectCreate,
    GradeSubjectResponse,
    GradeSubjectUpdate,
    PaginatedUsersResponse,
    ReferralApplyRequest,
    RoleStatsResponse,
    RoleUpdateRequest,
    StreamCreate,
    StreamResponse,
    StreamUpdate,
    UserProfileCreate,
    UserProfileResponse,
    UserProfileUpdate,
)
from sqlalchemy.orm import Session

from shared import events as ev
from shared.auth import require_role
from shared.messaging import publish

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


@router.get("/", response_model=PaginatedUsersResponse)
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    role: str | None = Query(None),
    search: str | None = Query(None),
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    """Admin: paginated list of all user profiles, with the email and active flag
    from this service's copy of each account (kept current by auth events)."""
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

    accounts = {
        a.user_id: a
        for a in db.query(ReplicaAccount).filter(
            ReplicaAccount.user_id.in_([p.user_id for p in profiles])
        )
    }
    items = []
    for p in profiles:
        account = accounts.get(p.user_id)
        items.append(
            _to_response(
                p,
                email=account.email if account else None,
                is_active=account.is_active if account else None,
            )
        )

    return PaginatedUsersResponse(
        total=total, page=page, page_size=page_size, items=items
    )


@router.post(
    "/", response_model=UserProfileResponse, status_code=status.HTTP_201_CREATED
)
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
            detail={
                "error": "PROFILE_EXISTS",
                "message": "Profile already exists for this user",
            },
        )

    profile, _ = create_profile(db, requester_id, requester_role, body)
    return _to_response(profile, email=payload.get("email"))


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


@router.post(
    "/streams", response_model=StreamResponse, status_code=status.HTTP_201_CREATED
)
async def create_stream(
    body: StreamCreate,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    stream = Stream(**body.model_dump())
    db.add(stream)
    db.commit()
    db.refresh(stream)
    announce_stream(stream)
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
    announce_stream(stream)
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
    publish(ev.USER_STREAM_DELETED, {"id": stream_id})


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


@router.post(
    "/grade-subjects",
    response_model=GradeSubjectResponse,
    status_code=status.HTTP_201_CREATED,
)
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
        stream = (
            db.query(Stream)
            .filter(Stream.id == stream_id, Stream.is_active.is_(True))
            .first()
        )  # noqa: E712
        return stream.subjects if stream else []

    rows = (
        db.query(GradeSubject)
        .filter(GradeSubject.grade == grade, GradeSubject.is_active.is_(True))
        .order_by(GradeSubject.name)
        .all()
    )
    return [r.name for r in rows]


def _own_profile(payload: dict, db: Session) -> UserProfile:
    profile = (
        db.query(UserProfile).filter(UserProfile.user_id == int(payload["sub"])).first()
    )
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


@router.get("/stats", response_model=RoleStatsResponse)
async def get_role_stats(
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    """Admin dashboard headline counts — cheap grouped COUNT query, no
    cross-service call needed since role lives on the profile itself."""
    return RoleStatsResponse(
        total_students=db.query(UserProfile)
        .filter(UserProfile.role == ProfileRole.student)
        .count(),
        total_teachers=db.query(UserProfile)
        .filter(UserProfile.role == ProfileRole.teacher)
        .count(),
        total_admins=db.query(UserProfile)
        .filter(UserProfile.role == ProfileRole.admin)
        .count(),
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

    # Email and active flag come from this service's copy of the account,
    # kept current by the events auth_service publishes.
    account = db.get(ReplicaAccount, user_id)
    email = payload.get("email") if requester_id == user_id else None
    return _to_response(
        profile,
        email=email or (account.email if account else None),
        is_active=account.is_active if account else None,
    )


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
    announce_profile(profile)
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
    announce_profile(profile)
    return _to_response(profile)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    """Admin only: delete a user profile. Other services hear about it through an event."""
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})

    db.delete(profile)
    db.commit()
    announce_profile_deleted(user_id)
