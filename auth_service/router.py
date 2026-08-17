import bcrypt
from database import SessionLocal
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from models import OtpCode, RefreshToken, User, UserRole
from schemas import (
    ErrorResponse,
    LoginRequest,
    OtpRequest,
    OtpVerifyRequest,
    RefreshRequest,
    TokenResponse,
    UserRegisterRequest,
    UserResponse,
)
from services import OtpService, SessionService, TokenService
from shared.auth import require_role
from sqlalchemy.orm import Session

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

    hashed = bcrypt.hashpw(req.password.encode("utf-8"), bcrypt.gensalt()).decode(
        "utf-8"
    )
    user = User(
        email=req.email,
        hashed_password=hashed,
        role=req.role,
        is_active=True,
        is_email_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


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

    role_str = user.role.value if isinstance(user.role, UserRole) else str(user.role)
    access_token = TokenService.create_access_token(user.id, role_str)
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
    new_access_token = TokenService.create_access_token(user.id, role_str)

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
