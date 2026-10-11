"""Admin dashboard numbers and the analytics page, composed from the services that own the data.

Courses stand in for ExamBuddy's subjects: Edura courses carry no separate subject.
"""

import asyncio
from datetime import datetime, timezone

import service_clients as clients


def _when(timestamp: str | None) -> datetime:
    if not timestamp:
        return datetime.min.replace(tzinfo=timezone.utc)
    moment = datetime.fromisoformat(timestamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def build_stats(src: dict, names: dict[int, str], titles: dict[int, str]) -> dict:
    users, assessments, payments = src["users"], src["assessments"], src["payments"]
    activity = []
    for s in users.get("recent_students", []):
        name = s.get("full_name") or "A student"
        activity.append(
            {
                "type": "student",
                "title": "New student registration",
                "subtitle": (
                    f"{name} joined from {s['district']}"
                    if s.get("district")
                    else f"{name} joined"
                ),
                "timestamp": s.get("created_at"),
            }
        )
    for p in payments.get("recent_verified", []):
        activity.append(
            {
                "type": "payment",
                "title": "Payment verified",
                "subtitle": f"Bank slip verified for {names.get(p['student_id'], 'Unknown')}",
                "timestamp": p.get("verified_at"),
            }
        )
    for e in assessments.get("recent_exams", []):
        course = titles.get(e["course_id"])
        activity.append(
            {
                "type": "exam",
                "title": "New exam uploaded",
                "subtitle": f"{e['title']} — {course}" if course else e["title"],
                "timestamp": e.get("created_at"),
            }
        )
    activity.sort(key=lambda item: _when(item["timestamp"]), reverse=True)

    return {
        "total_students": users.get("total_students", 0),
        "total_courses": len(src["courses"]),
        "total_exams": assessments.get("published_exams", 0),
        "total_exam_attempts": assessments.get("graded_attempts", 0),
        "total_revenue": payments.get("total_revenue", 0),
        "revenue_this_month": payments.get("revenue_this_month", 0),
        "pending_bank_slips": payments.get("pending_bank_slips", 0),
        "recent_activity": activity[:8],
    }


def build_analytics(src: dict, courses: dict[int, dict]) -> dict:
    """courses: {id: {"title", "grade"}} for every course the figures mention."""

    def title(course_id):
        return (courses.get(course_id) or {}).get("title") or "Untitled course"

    by_subject: dict[str, int] = {}
    for row in src["enrollments"]:
        if row["course_id"] not in courses:
            continue  # enrollments left behind by a deleted course
        by_subject[title(row["course_id"])] = (
            by_subject.get(title(row["course_id"]), 0) + row["count"]
        )

    return {
        "revenue_by_month": src["payments"].get("revenue_by_month", []),
        "students_by_grade": src["users"].get("by_grade", []),
        "students_by_district": src["users"].get("by_district", []),
        "attempts_by_month": src["assessments"].get("attempts_by_month", []),
        "top_exams": [
            {
                "exam_id": str(e["exam_id"]),
                "title": e["title"],
                "subject": title(e["course_id"]),
                "grade": (courses.get(e["course_id"]) or {}).get("grade"),
                "attempt_count": e["attempt_count"],
                "avg_score": e["avg_score"],
            }
            for e in src["assessments"].get("top_exams", [])
        ],
        "enrollments_by_subject": [
            {"subject": s, "count": c}
            for s, c in sorted(by_subject.items(), key=lambda item: (-item[1], item[0]))
            if c > 0
        ],
    }


async def _courses_for(src: dict, course_ids: set[int]) -> dict[int, dict]:
    """Published courses come with the sources; look up the rest (drafts, archived) one by one."""
    known = {c["id"]: c for c in src["courses"]}
    missing = [cid for cid in course_ids if cid not in known]
    for info in await asyncio.gather(*(clients.course_info(cid) for cid in missing)):
        if info:
            known[info["id"]] = info
    return known


async def admin_stats() -> dict:
    src = await clients.admin_sources()
    student_ids = [p["student_id"] for p in src["payments"].get("recent_verified", [])]
    course_ids = {e["course_id"] for e in src["assessments"].get("recent_exams", [])}
    names, courses = await asyncio.gather(
        clients.names(student_ids), _courses_for(src, course_ids)
    )
    return build_stats(src, names, {cid: c["title"] for cid, c in courses.items()})


async def admin_analytics() -> dict:
    src = await clients.admin_sources()
    course_ids = {e["course_id"] for e in src["assessments"].get("top_exams", [])}
    course_ids |= {row["course_id"] for row in src["enrollments"]}
    return build_analytics(src, await _courses_for(src, course_ids))
