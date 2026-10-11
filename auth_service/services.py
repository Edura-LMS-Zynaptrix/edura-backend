import hashlib
import json
import secrets
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import jwt
from config import settings
from fastapi import HTTPException
from models import RefreshToken
from sqlalchemy.orm import Session


# In-memory Redis fallback for testing environments where Redis server is offline
class MemoryRedisClient:
    def __init__(self):
        self._store: Dict[str, Tuple[str, Optional[datetime]]] = {}

    def set(self, name: str, value: str, ex: Optional[int] = None):
        exp = datetime.now(timezone.utc) + timedelta(seconds=ex) if ex else None
        self._store[name] = (str(value), exp)

    def get(self, name: str) -> Optional[str]:
        if name not in self._store:
            return None
        val, exp = self._store[name]
        if exp and datetime.now(timezone.utc) > exp:
            del self._store[name]
            return None
        return val

    def delete(self, name: str):
        self._store.pop(name, None)

    def keys(self, pattern: str) -> List[str]:
        # Simple pattern matching for session:{user_id}:*
        prefix = pattern.replace("*", "")
        now = datetime.now(timezone.utc)
        valid_keys = []
        for k, (_v, exp) in list(self._store.items()):
            if exp and now > exp:
                del self._store[k]
                continue
            if k.startswith(prefix):
                valid_keys.append(k)
        return valid_keys


try:
    import redis

    redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    redis_client.ping()
except Exception:
    redis_client = MemoryRedisClient()


class TokenService:
    @staticmethod
    def create_access_token(
        user_id: int, role: str, email: Optional[str] = None
    ) -> str:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
        payload = {
            "sub": str(user_id),
            "role": role.upper(),
            "exp": expire,
            "iat": datetime.now(timezone.utc),
        }
        if email:
            payload["email"] = email
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    @staticmethod
    def create_refresh_token(db: Session, user_id: int) -> str:
        token_str = secrets.token_hex(32)
        token_hash = hashlib.sha256(token_str.encode()).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )

        db_token = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            is_revoked=False,
            expires_at=expires_at,
        )
        db.add(db_token)
        db.commit()
        return token_str

    @staticmethod
    def rotate_refresh_token(db: Session, token_str: str) -> Tuple[int, str]:
        token_hash = hashlib.sha256(token_str.encode()).hexdigest()
        db_token = (
            db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
        )

        if not db_token:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "INVALID_REFRESH_TOKEN",
                    "message": "Invalid refresh token",
                },
            )

        if db_token.is_revoked:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "REFRESH_TOKEN_REUSE",
                    "message": "Refresh token reuse detected",
                },
            )

        # Check expiration
        exp = db_token.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "REFRESH_TOKEN_EXPIRED",
                    "message": "Refresh token expired",
                },
            )

        # Revoke old token
        db_token.is_revoked = True
        db.commit()

        # Create new refresh token
        new_token_str = TokenService.create_refresh_token(db, db_token.user_id)
        return db_token.user_id, new_token_str

    @staticmethod
    def revoke_refresh_token(db: Session, token_str: str) -> None:
        token_hash = hashlib.sha256(token_str.encode()).hexdigest()
        db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).update(
            {RefreshToken.is_revoked: True}
        )
        db.commit()

    @staticmethod
    def revoke_all_for_user(db: Session, user_id: int) -> None:
        db.query(RefreshToken).filter(
            RefreshToken.user_id == user_id, RefreshToken.is_revoked.is_(False)
        ).update({RefreshToken.is_revoked: True})
        db.commit()


class GoogleAuthService:
    """
    Verifies a Google OAuth *access token* (implicit flow — the frontend uses
    @react-oauth/google's useGoogleLogin({flow: 'implicit'}), not one-tap
    id_tokens) by calling Google's userinfo endpoint directly. No client
    secret or google-auth library needed for this flow.
    """

    USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

    @staticmethod
    def verify_access_token(access_token: str) -> Optional[dict]:
        try:
            req = urllib.request.Request(
                GoogleAuthService.USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())

            email = data.get("email")
            if not email:
                return None

            return {
                "google_id": data.get("sub", ""),
                "email": email,
                "full_name": data.get("name", ""),
                "picture": data.get("picture", ""),
            }
        except (urllib.error.HTTPError, urllib.error.URLError, Exception):
            return None


class PasswordResetService:
    """
    Stateless reset links: a signed JWT that also carries a fingerprint of the
    current password hash, so the link stops working once it has been used
    (or the password changes some other way) — no token table needed.
    """

    PURPOSE = "password_reset"

    @staticmethod
    def _fingerprint(hashed_password: Optional[str]) -> str:
        return hashlib.sha256((hashed_password or "no-password").encode()).hexdigest()[
            :16
        ]

    @staticmethod
    def create_token(user_id: int, hashed_password: Optional[str]) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user_id),
            "purpose": PasswordResetService.PURPOSE,
            "pwd": PasswordResetService._fingerprint(hashed_password),
            "iat": now,
            "exp": now + timedelta(minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES),
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    @staticmethod
    def read_token(token: str) -> Optional[Tuple[int, str]]:
        """Returns (user_id, password fingerprint) or None when invalid/expired."""
        try:
            data = jwt.decode(
                token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
            )
        except jwt.PyJWTError:
            return None
        if data.get("purpose") != PasswordResetService.PURPOSE:
            return None
        try:
            return int(data["sub"]), str(data.get("pwd", ""))
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def matches(hashed_password: Optional[str], fingerprint: str) -> bool:
        return secrets.compare_digest(
            PasswordResetService._fingerprint(hashed_password), fingerprint
        )


class SessionService:
    MAX_SESSIONS = 3

    @staticmethod
    def delete_all(user_id: int) -> None:
        for key in redis_client.keys(f"session:{user_id}:*"):
            redis_client.delete(key)

    @staticmethod
    def create_session(user_id: int, role: str) -> str:
        session_id = str(uuid.uuid4())
        pattern = f"session:{user_id}:*"

        # Check active sessions count
        existing_keys = redis_client.keys(pattern)
        if len(existing_keys) >= SessionService.MAX_SESSIONS:
            # Evict oldest key
            oldest_key = sorted(existing_keys)[0]
            redis_client.delete(oldest_key)

        key = f"session:{user_id}:{session_id}"
        session_data = f'{{"role": "{role}", "issued_at": "{datetime.now(timezone.utc).isoformat()}"}}'
        # Set session with 7-day expiry
        redis_client.set(key, session_data, ex=7 * 24 * 3600)
        return session_id


class OtpService:
    OTP_TTL_SECONDS = 300  # 5 minutes

    @staticmethod
    def generate_otp(user_id: int, purpose: str = "email_verify") -> str:
        code = f"{secrets.randbelow(1000000):06d}"
        key = f"otp:{purpose}:{user_id}"
        redis_client.set(key, code, ex=OtpService.OTP_TTL_SECONDS)
        return code

    @staticmethod
    def verify_otp(user_id: int, code: str, purpose: str = "email_verify") -> bool:
        key = f"otp:{purpose}:{user_id}"
        stored_code = redis_client.get(key)
        if not stored_code:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "OTP_EXPIRED",
                    "message": "OTP has expired or does not exist",
                },
            )
        if stored_code != code:
            raise HTTPException(
                status_code=400,
                detail={"error": "INVALID_OTP", "message": "Incorrect OTP code"},
            )
        redis_client.delete(key)
        return True
