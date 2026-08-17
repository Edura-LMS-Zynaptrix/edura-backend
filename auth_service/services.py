import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from config import settings
from fastapi import HTTPException
from jose import jwt
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
    def create_access_token(user_id: int, role: str) -> str:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
        payload = {
            "sub": str(user_id),
            "role": role.upper(),
            "exp": expire,
            "iat": datetime.now(timezone.utc),
        }
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


class SessionService:
    MAX_SESSIONS = 3

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
