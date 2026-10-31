import base64
import json
import logging
import os
import re

import dashboard
import progress
import redis
from events import publish_event
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from readmodel import SourceUnavailable, account, fetch_platform_sources
from report import generate_report_pdf, now_local

from shared.auth import require_role

logger = logging.getLogger(__name__)

router = APIRouter()

_STATS_CACHE_KEY = "analytics:platform-stats"
_STATS_TTL_SECONDS = 300
_SUMMARY_CACHE_KEY = "analytics:progress-summary"
_SUMMARY_TTL_SECONDS = 60
_ADMIN_TTL_SECONDS = 30

try:
    _redis = redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        decode_responses=True,
        socket_timeout=2,
    )
except Exception:  # pragma: no cover - construction never touches the network
    _redis = None


def _cache_get(key: str = _STATS_CACHE_KEY):
    try:
        raw = _redis.get(key) if _redis else None
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.warning("Cache read failed (%s): %s", key, e)
        return None


def _cache_set(
    value, key: str = _STATS_CACHE_KEY, ttl: int = _STATS_TTL_SECONDS
) -> None:
    try:
        if _redis:
            _redis.set(key, json.dumps(value), ex=ttl)
    except Exception as e:
        logger.warning("Cache write failed (%s): %s", key, e)


def _unavailable(e: SourceUnavailable) -> HTTPException:
    logger.error("Progress view unavailable: %s", e)
    return HTTPException(status_code=503, detail={"error": "SOURCE_UNAVAILABLE"})


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
    enrollment_counts = {
        row["course_id"]: row["count"] for row in sources["enrollments"]
    }

    ranked = sorted(
        courses, key=lambda c: enrollment_counts.get(c["id"], 0), reverse=True
    )
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


# ---------------------------------------------------------------------------
# Student progress (static /students/progress-summary sits above /students/{user_id}/...)
# ---------------------------------------------------------------------------


@router.get("/students/progress-summary")
async def students_progress_summary(_: dict = Depends(require_role(["admin"]))):
    cached = _cache_get(_SUMMARY_CACHE_KEY)
    if cached is not None:
        return cached
    try:
        summary = await progress.progress_summary()
    except SourceUnavailable as e:
        raise _unavailable(e)
    _cache_set(summary, _SUMMARY_CACHE_KEY, _SUMMARY_TTL_SECONDS)
    return summary


async def _progress_or_404(user_id: int) -> dict:
    try:
        data = await progress.student_progress(user_id)
    except SourceUnavailable as e:
        raise _unavailable(e)
    if data is None:
        raise HTTPException(status_code=404, detail={"error": "STUDENT_NOT_FOUND"})
    return data


@router.get("/students/{user_id}/progress")
async def student_progress(user_id: int, _: dict = Depends(require_role(["admin"]))):
    return await _progress_or_404(user_id)


@router.post("/students/{user_id}/send-report")
async def send_student_report(user_id: int, _: dict = Depends(require_role(["admin"]))):
    """Email the student a PDF of their progress (notification_service delivers it)."""
    data = await _progress_or_404(user_id)
    try:
        user = await account(user_id)
    except SourceUnavailable as e:
        raise _unavailable(e)
    if not user or not user.get("email"):
        raise HTTPException(status_code=404, detail={"error": "STUDENT_NOT_FOUND"})

    pdf = await run_in_threadpool(generate_report_pdf, data)
    now = now_local()
    name = data["student"]["full_name"] or "Student"
    published = await run_in_threadpool(
        publish_event,
        "report.progress",
        {
            "user_id": user_id,
            "email": user["email"],
            "student_name": name,
            "month": now.strftime("%B %Y"),
            "filename": f"Progress_Report_{re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_')}_{now.strftime('%Y%m%d')}.pdf",
            "pdf_base64": base64.b64encode(pdf).decode("ascii"),
        },
    )
    if not published:
        raise HTTPException(status_code=503, detail={"error": "REPORT_NOT_QUEUED"})
    return {"message": f"Progress report sent to {user['email']}"}


@router.get("/me/progress")
async def my_progress(payload: dict = Depends(require_role(["student"]))):
    return await _progress_or_404(int(payload["sub"]))


# ---------------------------------------------------------------------------
# Course struggle analysis
# ---------------------------------------------------------------------------


@router.get("/courses/{course_id}/struggle")
async def course_struggle(course_id: int, _: dict = Depends(require_role(["admin"]))):
    try:
        data = await progress.course_struggle(course_id)
    except SourceUnavailable as e:
        raise _unavailable(e)
    if data is None:
        raise HTTPException(status_code=404, detail={"error": "COURSE_NOT_FOUND"})
    return data


# ---------------------------------------------------------------------------
# Admin dashboard and analytics page
# ---------------------------------------------------------------------------


async def _admin_view(key: str, build):
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        view = await build()
    except SourceUnavailable as e:
        raise _unavailable(e)
    _cache_set(view, key, _ADMIN_TTL_SECONDS)
    return view


@router.get("/admin/stats")
async def admin_stats(_: dict = Depends(require_role(["admin"]))):
    return await _admin_view("analytics:admin-stats", dashboard.admin_stats)


@router.get("/admin/analytics")
async def admin_analytics(_: dict = Depends(require_role(["admin"]))):
    return await _admin_view("analytics:admin-analytics", dashboard.admin_analytics)
