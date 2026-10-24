import bcrypt
from config import settings
from database import SessionLocal
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from models import User, UserRole
from schemas import (
    ChangePasswordRequest,
    CreateAdminRequest,
    CurrentUserResponse,
    ForgotPasswordRequest,
    GoogleLoginRequest,
    LoginRequest,
    OtpRequest,
    OtpVerifyRequest,
    RefreshRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    SetPasswordRequest,
    TokenResponse,
    UserRegisterRequest,
    UserResponse,
    UserStatusUpdateRequest,
    VerifyEmailRequest,
)
from services import (
    EmailVerificationService,
    GoogleAuthService,
    OtpService,
    PasswordResetService,
    SessionService,
    TokenService,
)
from sqlalchemy.orm import Session

from shared import events as ev
from shared.auth import require_role
from shared.messaging import publish as publish_event

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


def _send_verification_email(user: User) -> None:
    token = EmailVerificationService.create_token(user.id, user.email)
    publish_event(
        "auth.email-verification-requested",
        {
            "user_id": user.id,
            "email": user.email,
            "verify_url": f"{settings.FRONTEND_URL.rstrip('/')}/verify-email?token={token}",
            "expires_in_minutes": settings.EMAIL_VERIFICATION_EXPIRE_MINUTES,
        },
    )


def announce_account(user: User) -> None:
    """Tell the other services this account changed (they keep their own copy of it)."""
    publish_event(
        ev.AUTH_ACCOUNT_UPDATED,
        {
            "user_id": user.id,
            "email": user.email,
            "role": (
                user.role.value if isinstance(user.role, UserRole) else str(user.role)
            ),
            "is_active": user.is_active,
            "is_email_verified": user.is_email_verified,
        },
    )


def _announce_registration(user: User, profile: dict | None) -> None:
    """A new account exists. user_service stores the profile that came with it."""
    publish_event(
        ev.AUTH_USER_REGISTERED,
        {
            "user_id": user.id,
            "email": user.email,
            "role": (
                user.role.value if isinstance(user.role, UserRole) else str(user.role)
            ),
            "is_active": user.is_active,
            "is_email_verified": user.is_email_verified,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "profile": profile,
        },
    )


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

    # user_service creates the profile when it receives this event. The form was
    # already validated by the request schema, so there is nothing to roll back.
    _announce_registration(
        user,
        req.profile.model_dump(mode="json", exclude_none=True) if req.profile else None,
    )
    _send_verification_email(user)
    return user


@router.post("/verify-email")
def verify_email(req: VerifyEmailRequest, db: Session = Depends(get_db)):
    """Marks the account behind a verification link as verified."""
    parsed = EmailVerificationService.read_token(req.token)
    user = db.query(User).filter(User.id == parsed[0]).first() if parsed else None
    # The link is only good for the address it was sent to.
    if not user or user.email != parsed[1]:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_VERIFICATION_TOKEN",
                "message": "This verification link is invalid or has expired. Request a new one.",
            },
        )
    if not user.is_email_verified:
        user.is_email_verified = True
        db.commit()
        announce_account(user)
    return {"message": "Email verified. You can now log in."}


@router.post("/resend-verification")
def resend_verification(req: ResendVerificationRequest, db: Session = Depends(get_db)):
    """Sends a fresh link. Same answer for every address, so accounts can't be probed."""
    user = db.query(User).filter(User.email == req.email).first()
    if (
        user
        and user.is_active
        and user.hashed_password
        and not user.is_email_verified
        and EmailVerificationService.may_resend(user.id)
    ):
        _send_verification_email(user)
    return {"message": _VERIFICATION_REQUESTED_MESSAGE}


@router.post("/admin/create-admin", response_model=UserResponse, status_code=201)
def create_admin(
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

    hashed = bcrypt.hashpw(req.password.encode("utf-8"), bcrypt.gensalt()).decode(
        "utf-8"
    )
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

    _announce_registration(
        user, {"first_name": req.first_name, "last_name": req.last_name}
    )
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
    announce_account(user)
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

    # Checked only after the password is right, so it can't be used to learn
    # which addresses are registered.
    if not user.is_email_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "EMAIL_NOT_VERIFIED",
                "message": "Please verify your email address before signing in. Check your inbox for the verification link.",
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
async def google_login(
    req: GoogleLoginRequest, response: Response, db: Session = Depends(get_db)
):
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
            detail={
                "error": "INVALID_GOOGLE_TOKEN",
                "message": "Invalid or expired Google token",
            },
        )

    google_id = google_info["google_id"]
    email = google_info["email"]

    user = db.query(User).filter(User.google_id == google_id).first()
    if not user:
        user = db.query(User).filter(User.email == email).first()
    verified_now = False

    if user:
        if not user.google_id:
            user.google_id = google_id
            if not user.hashed_password:
                user.auth_provider = "google"
        if google_info.get("email_verified") and not user.is_email_verified:
            # Google has confirmed this address belongs to the person signing in.
            # A password on a never-verified account was chosen by whoever typed
            # the address in first, who may not be the owner: remove it.
            user.is_email_verified = True
            user.hashed_password = None
            user.auth_provider = "google"
            verified_now = True
        db.commit()
        if verified_now:
            announce_account(user)
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
        first_name, _, last_name = (
            (google_info.get("full_name") or "").strip().partition(" ")
        )
        _announce_registration(
            user,
            {
                "first_name": first_name or email.split("@")[0],
                "last_name": last_name.strip(),
                "avatar_url": google_info.get("picture") or None,
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
            detail={
                "error": "PASSWORD_ALREADY_SET",
                "message": "A password is already set. Use change-password instead.",
            },
        )

    user.hashed_password = bcrypt.hashpw(
        req.new_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")
    db.commit()
    return {
        "message": "Password set successfully. You can now log in with email and password."
    }


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
            detail={
                "error": "NO_PASSWORD_SET",
                "message": "No password set yet. Use set-password instead.",
            },
        )
    if not bcrypt.checkpw(
        req.current_password.encode("utf-8"), user.hashed_password.encode("utf-8")
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_CREDENTIALS",
                "message": "Current password is incorrect",
            },
        )

    user.hashed_password = bcrypt.hashpw(
        req.new_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")
    db.commit()
    return {"message": "Password updated successfully"}


_VERIFICATION_REQUESTED_MESSAGE = "If that address has an account waiting to be verified, a new verification link is on its way."
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
    if (
        not user
        or not user.is_active
        or not PasswordResetService.matches(user.hashed_password, fingerprint)
    ):
        raise invalid

    user.hashed_password = bcrypt.hashpw(
        req.new_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")
    # Following the emailed link shows the person controls this address.
    newly_verified = not user.is_email_verified
    user.is_email_verified = True
    db.commit()
    if newly_verified:
        announce_account(user)

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
