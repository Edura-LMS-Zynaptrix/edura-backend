import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Union

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_URL = os.getenv("REDIS_URL", f"redis://{REDIS_HOST}:{REDIS_PORT}/0")


class MemoryRedisClient:
    def __init__(self):
        self._store: Dict[str, Tuple[str, Optional[datetime]]] = {}
        # Dict[key, Dict[member_str, float]]
        self._zsets: Dict[str, Dict[str, float]] = {}

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
        self._zsets.pop(name, None)

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
        for k in self._zsets.keys():
            if k.startswith(prefix) and k not in valid_keys:
                valid_keys.append(k)
        return valid_keys

    # Sorted Set implementation for in-memory fallback
    def zadd(self, name: str, mapping: Dict[str, float]) -> int:
        if name not in self._zsets:
            self._zsets[name] = {}
        added = 0
        for member, score in mapping.items():
            member_str = str(member)
            if member_str not in self._zsets[name]:
                added += 1
            self._zsets[name][member_str] = float(score)
        return added

    def zincrby(self, name: str, amount: float, value: str) -> float:
        if name not in self._zsets:
            self._zsets[name] = {}
        member_str = str(value)
        current = self._zsets[name].get(member_str, 0.0)
        new_score = current + float(amount)
        self._zsets[name][member_str] = new_score
        return new_score

    def zscore(self, name: str, member: str) -> Optional[float]:
        if name not in self._zsets:
            return None
        return self._zsets[name].get(str(member))

    def zrevrange(
        self, name: str, start: int, end: int, withscores: bool = False
    ) -> Union[List[str], List[Tuple[str, float]]]:
        if name not in self._zsets:
            return []
        items = list(self._zsets[name].items())
        # Sort descending by score, then member name ascending for consistency
        items.sort(key=lambda x: (-x[1], x[0]))

        if end < 0:
            end = len(items) + end
        sliced = items[start : end + 1]

        if withscores:
            return [(member, score) for member, score in sliced]
        return [member for member, score in sliced]


try:
    import redis

    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
except Exception:
    redis_client = MemoryRedisClient()


def get_redis_client():
    return redis_client
