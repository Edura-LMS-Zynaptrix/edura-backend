import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_URL = os.getenv("REDIS_URL", f"redis://{REDIS_HOST}:{REDIS_PORT}/0")


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

    def ttl(self, name: str) -> int:
        if name not in self._store:
            return -2
        val, exp = self._store[name]
        if exp is None:
            return -1
        now = datetime.now(timezone.utc)
        if now > exp:
            del self._store[name]
            return -2
        return int((exp - now).total_seconds())

    def delete(self, name: str):
        self._store.pop(name, None)

    def keys(self, pattern: str) -> List[str]:
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

    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
except Exception:
    redis_client = MemoryRedisClient()


def get_redis_client():
    return redis_client
