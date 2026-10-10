import json
import logging
import os

import redis
from fastapi import APIRouter, Depends
from service_clients import fetch_platform_sources

from shared.auth import require_role

logger = logging.getLogger(__name__)

router = APIRouter()

_STATS_CACHE_KEY = "analytics:platform-stats"
_STATS_TTL_SECONDS = 300

try:
    _redis = redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        decode_responses=True,
        socket_timeout=2,
    )
except Exception:  # pragma: no cover - construction never touches the network
    _redis = None


def _cache_get():
    try:
        raw = _redis.get(_STATS_CACHE_KEY) if _redis else None
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.warning("Stats cache read failed: %s", e)
        return None


def _cache_set(value: dict) -> None:
    try:
        if _redis:
            _redis.set(_STATS_CACHE_KEY, json.dumps(value), ex=_STATS_TTL_SECONDS)
    except Exception as e:
        logger.warning("Stats cache write failed: %s", e)


@router.get("/analytics")
async def analytics_only(_: None = Depends(require_role(["admin"]))):
    return {"message": "Welcome Admin"}


@router.get("/platform-stats")
async def platform_stats():
    """Public homepage numbers, aggregated from the owning services and cached for 5 minutes."""
    cached = _cache_get()
    if cached:
        return cached

    sources = await fetch_platform_sources()
    courses = sources["courses"]
    enrollment_counts = {row["course_id"]: row["count"] for row in sources["enrollments"]}

    ranked = sorted(courses, key=lambda c: enrollment_counts.get(c["id"], 0), reverse=True)
    stats = {
        "total_students": sources["users"].get("total_students", 0),
        "total_courses": len(courses),
        "total_exams": sources["assessments"].get("published_exams", 0),
        "total_attempts": sources["assessments"].get("graded_attempts", 0),
        "top_courses": [
            {
                "id": str(c["id"]),
                "title": c["title"],
                "subject": "",
                "grade": c.get("grade") or 0,
                "image_url": c.get("thumbnail_url"),
                "price": float(c.get("price") or 0),
                "course_type": c.get("course_type", "video"),
                "enrollment_count": enrollment_counts.get(c["id"], 0),
                "stream_ids": [str(s) for s in (c.get("stream_ids") or [])],
            }
            for c in ranked[:4]
        ],
    }
    _cache_set(stats)
    return stats
