import bcrypt
from database import SessionLocal
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from models import User, UserRole
from schemas import (
    ChangePasswordRequest,
    CreateAdminRequest,
    CurrentUserResponse,
    ForgotPasswordRequest,
    GoogleLoginRequest,
    InternalUserResponse,
    LoginRequest,
    OtpRequest,
    OtpVerifyRequest,
    RefreshRequest,
    ResetPasswordRequest,
    SetPasswordRequest,
    TokenResponse,
    UserRegisterRequest,
    UserResponse,
    UserStatusUpdateRequest,
)
from config import settings
from events import publish_event
from services import (
    GoogleAuthService,
    OtpService,
    PasswordResetService,
    SessionService,
    TokenService,
)
from sqlalchemy.orm import Session
from user_client import create_profile_internal

from shared.auth import require_role

router = APIRouter(tags=["auth"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/admin")
def admin_only(payload: dict = Depends(require_role(["admin"]))):
    return {"message": "Welcome Admin", "user_id": payload.get("sub")}


@router.post("/register", response_model=UserResponse)
def register(req: UserRegisterRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail={"error": "EMAIL_EXISTS", "message": "Email already registered"},
        )

    # Public self-registration can only ever create student/teacher accounts.
    # Admin accounts are created out-of-band (create_master_admin.py, or by an
    # existing admin via POST /admin/create-admin below) — never accept
    # role=admin from a client request here.
    role = req.role if req.role != UserRole.admin else UserRole.student

    hashed = bcrypt.hashpw(req.password.encode("utf-8"), bcrypt.gensalt()).decode(
        "utf-8"
    )
    user = User(
        email=req.email,
        hashed_password=hashed,
        role=role,
        is_active=True,
        is_email_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/internal/users", response_model=list[InternalUserResponse])
def list_users_internal(db: Session = Depends(get_db)):
    """
    Unauthenticated, service-to-service only (mirrors course_service's
    /owner and content_service's /internal/receipt-upload). Lets user_service
    enrich its admin-facing profile listings with is_active/email — fields
    that only exist on this service's User row, not on a UserProfile.
    """
    return db.query(User).all()


@router.get("/internal/users/{user_id}", response_model=InternalUserResponse)
def get_user_internal(user_id: int, db: Session = Depends(get_db)):
    """Singular counterpart to /internal/users — used where only one
    account's is_active/email is needed (e.g. user_service's GET /{user_id})
    instead of pulling the whole table for a single lookup."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})
    return user


@router.post("/admin/create-admin", response_model=UserResponse, status_code=201)
async def create_admin(
    req: CreateAdminRequest,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    """
    The only way to create another admin account short of the offline
    create_master_admin.py script — public /register can never do this
    (see the role downgrade above). Also creates the matching user_service
    profile so the new admin shows up immediately, with a name, in the
    admin list — unlike a self-registered account, nobody will ever log in
    as this user to run the normal register->login->createProfile flow.
    """
    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail={"error": "EMAIL_EXISTS", "message": "Email already registered"},
        )

    hashed = bcrypt.hashpw(req.password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user = User(
        email=req.email,
        hashed_password=hashed,
        role=UserRole.admin,
        is_active=True,
        is_email_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    await create_profile_internal(user.id, "admin", req.first_name, req.last_name)

    return user


@router.put("/users/{user_id}/status")
def update_user_status(
    user_id: int,
    body: UserStatusUpdateRequest,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})

    user.is_active = body.is_active
    db.commit()
    return {"user_id": user.id, "is_active": user.is_active}


@router.delete("/users/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    """
    Deletes the login record. Paired with user_service's own DELETE
    /{user_id} for the profile — the admin frontend calls both so a
    "delete student/admin" action removes the account everywhere, not just
    its profile (which would otherwise leave a login-only account behind
    that could still authenticate but show up nowhere).
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})

    db.delete(user)
    db.commit()


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email).first()
    if not user:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "INVALID_CREDENTIALS",
                "message": "Email or password is incorrect",
            },
        )

    # Google-only accounts have no password to check against.
    if not user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "GOOGLE_ONLY_ACCOUNT",
                "message": "This account uses Google Sign-In. Please continue with Google, or set a password from your profile after signing in.",
            },
        )

    if not bcrypt.checkpw(
        req.password.encode("utf-8"), user.hashed_password.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "INVALID_CREDENTIALS",
                "message": "Email or password is incorrect",
            },
        )

    if not user.is_active:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "ACCOUNT_DEACTIVATED",
                "message": "This account has been deactivated. Please contact support.",
            },
        )

    role_str = user.role.value if isinstance(user.role, UserRole) else str(user.role)
    access_token = TokenService.create_access_token(user.id, role_str, user.email)
    refresh_token = TokenService.create_refresh_token(db, user.id)

    # Session limit management in Redis
    SessionService.create_session(user.id, role_str)

    # Set HttpOnly refresh token cookie
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
    )

    return TokenResponse(access_token=access_token, token_type="Bearer", expires_in=900)


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    req: RefreshRequest = None,
    refresh_token: str = Cookie(None),
    response: Response = None,
    db: Session = Depends(get_db),
):
    token_val = (
        req.refresh_token if req and req.refresh_token else None
    ) or refresh_token
    if not token_val:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "MISSING_REFRESH_TOKEN",
                "message": "Refresh token not provided",
            },
        )

    user_id, new_refresh_token = TokenService.rotate_refresh_token(db, token_val)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "USER_NOT_FOUND",
                "message": "User associated with refresh token not found",
            },
        )

    role_str = user.role.value if isinstance(user.role, UserRole) else str(user.role)
    new_access_token = TokenService.create_access_token(user.id, role_str, user.email)

    if response:
        response.set_cookie(
            key="refresh_token",
            value=new_refresh_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=7 * 24 * 3600,
        )

    return TokenResponse(
        access_token=new_access_token, token_type="Bearer", expires_in=900
    )


@router.post("/google", response_model=TokenResponse)
async def google_login(req: GoogleLoginRequest, response: Response, db: Session = Depends(get_db)):
    """
    Sign in (or sign up) with Google. Unifies with an existing email+password
    account: looked up by google_id first, then by email — if a password
    account with the same email exists, this links google_id onto it instead
    of creating a duplicate, so the same account works with both methods.
    """
    google_info = GoogleAuthService.verify_access_token(req.access_token)
    if not google_info:
        raise HTTPException(
            status_code=401,
            detail={"error": "INVALID_GOOGLE_TOKEN", "message": "Invalid or expired Google token"},
        )

    google_id = google_info["google_id"]
    email = google_info["email"]

    user = db.query(User).filter(User.google_id == google_id).first()
    if not user:
        user = db.query(User).filter(User.email == email).first()

    if user:
        if not user.google_id:
            user.google_id = google_id
            if not user.hashed_password:
                user.auth_provider = "google"
        db.commit()
    else:
        user = User(
            email=email,
            hashed_password=None,
            role=UserRole.student,
            is_active=True,
            is_email_verified=True,
            google_id=google_id,
            auth_provider="google",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        # Seed the profile from Google, as the reference platform does, so
        # complete-profile only has to collect the academic details.
        first_name, _, last_name = (google_info.get("full_name") or "").strip().partition(" ")
        await create_profile_internal(
            user.id,
            "student",
            first_name or email.split("@")[0],
            last_name.strip(),
            avatar_url=google_info.get("picture") or None,
        )

    if not user.is_active:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "ACCOUNT_DEACTIVATED",
                "message": "This account has been deactivated. Please contact support.",
            },
        )

    role_str = user.role.value if isinstance(user.role, UserRole) else str(user.role)
    access_token = TokenService.create_access_token(user.id, role_str, user.email)
    refresh_token = TokenService.create_refresh_token(db, user.id)

    SessionService.create_session(user.id, role_str)

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
    )

    return TokenResponse(access_token=access_token, token_type="Bearer", expires_in=900)


@router.get("/me", response_model=CurrentUserResponse)
def get_me(
    payload: dict = Depends(require_role(["student", "teacher", "admin"])),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})
    return CurrentUserResponse(
        id=user.id,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        is_email_verified=user.is_email_verified,
        auth_provider=user.auth_provider,
        has_password=user.hashed_password is not None,
    )


@router.post("/set-password")
def set_password(
    req: SetPasswordRequest,
    payload: dict = Depends(require_role(["student", "teacher", "admin"])),
    db: Session = Depends(get_db),
):
    """Let a Google-only account add a password, so it can log in either way."""
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})
    if user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail={"error": "PASSWORD_ALREADY_SET", "message": "A password is already set. Use change-password instead."},
        )

    user.hashed_password = bcrypt.hashpw(
        req.new_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")
    db.commit()
    return {"message": "Password set successfully. You can now log in with email and password."}


@router.post("/change-password")
def change_password(
    req: ChangePasswordRequest,
    payload: dict = Depends(require_role(["student", "teacher", "admin"])),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "USER_NOT_FOUND"})
    if not user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail={"error": "NO_PASSWORD_SET", "message": "No password set yet. Use set-password instead."},
        )
    if not bcrypt.checkpw(
        req.current_password.encode("utf-8"), user.hashed_password.encode("utf-8")
    ):
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_CREDENTIALS", "message": "Current password is incorrect"},
        )

    user.hashed_password = bcrypt.hashpw(
        req.new_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")
    db.commit()
    return {"message": "Password updated successfully"}


_RESET_REQUESTED_MESSAGE = (
    "If an account with that email exists, a password reset link has been sent."
)


@router.post("/forgot-password")
def forgot_password(req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Emails a one-hour reset link. Same response either way, so emails can't be probed."""
    user = db.query(User).filter(User.email == req.email).first()
    if user and user.is_active:
        token = PasswordResetService.create_token(user.id, user.hashed_password)
        publish_event(
            "auth.password-reset-requested",
            {
                "user_id": user.id,
                "email": user.email,
                "reset_url": f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?token={token}",
                "expires_in_minutes": settings.PASSWORD_RESET_EXPIRE_MINUTES,
            },
        )
    return {"message": _RESET_REQUESTED_MESSAGE}


@router.post("/reset-password")
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    invalid = HTTPException(
        status_code=400,
        detail={
            "error": "INVALID_RESET_TOKEN",
            "message": "Invalid or expired reset link. Please request a new one.",
        },
    )
    parsed = PasswordResetService.read_token(req.token)
    if not parsed:
        raise invalid
    user_id, fingerprint = parsed
    user = db.query(User).filter(User.id == user_id).first()
    # The fingerprint changes with the password, so a used link can't be replayed.
    if not user or not user.is_active or not PasswordResetService.matches(user.hashed_password, fingerprint):
        raise invalid

    user.hashed_password = bcrypt.hashpw(
        req.new_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")
    db.commit()

    # Anyone holding an old session is signed out.
    TokenService.revoke_all_for_user(db, user.id)
    SessionService.delete_all(user.id)
    publish_event("auth.password-changed", {"user_id": user.id, "email": user.email})
    return {"message": "Password reset successfully. You can now log in."}


@router.post("/logout")
def logout(
    response: Response,
    refresh_token: str = Cookie(None),
    db: Session = Depends(get_db),
):
    """Revokes this browser's refresh token and clears its cookie."""
    if refresh_token:
        TokenService.revoke_refresh_token(db, refresh_token)
    response.delete_cookie("refresh_token", httponly=True, secure=True, samesite="lax")
    return {"message": "Successfully logged out"}


@router.post("/otp/request")
def request_otp(req: OtpRequest):
    code = OtpService.generate_otp(req.user_id, req.purpose.value)
    return {"message": "OTP sent successfully", "code": code}


@router.post("/otp/verify")
def verify_otp(req: OtpVerifyRequest):
    OtpService.verify_otp(req.user_id, req.code, req.purpose.value)
    return {"message": "OTP verified successfully"}


@router.get("/protected")
def protected_route(
    payload: dict = Depends(require_role(["student", "teacher", "admin"])),
):
    return {
        "message": "Access granted",
        "user_id": payload.get("sub"),
        "role": payload.get("role"),
    }
