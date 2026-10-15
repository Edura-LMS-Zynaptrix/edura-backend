"""Student progress and course struggle views, composed from the services that own the data.

enrollment_service says what a student can open, assessment_service holds the
exams and attempts, course_service the titles and lessons, user_service the
profile. Nothing here is stored; every view is rebuilt from those sources.
"""

import asyncio

import service_clients as clients

PASS_MARK_PCT = 85  # an attempt counts as passed at or above this score
FAIL_MARK_PCT = 40  # a first attempt below this score counts as a failure


def _avg(values: list) -> int:
    return round(sum(values) / len(values)) if values else 0


def build_student_progress(
    profile: dict, exams: list[dict], titles: dict[int, str], badges: int
) -> dict:
    """Subject-wise progress over every exam the student can take. Courses stand in for subjects."""
    subjects: dict[int, dict] = {}
    for exam in exams:
        course_id = exam["course_id"]
        title = titles.get(course_id, "General")
        s = subjects.setdefault(
            course_id,
            {
                "subject": title,
                "course_title": title,
                "total_exams": 0,
                "attempted_exams": 0,
                "total_attempts_count": 0,
                "best_score_pct": 0,
                "avg_score_pct": 0,
                "avg_time_seconds": 0,
                "exams": [],
                "all_scores": [],
                "all_times": [],
            },
        )
        s["total_exams"] += 1

        history = []
        for number, attempt in enumerate(exam["attempts"], start=1):  # oldest first
            score = attempt["score_pct"]
            s["all_scores"].append(score)
            if attempt.get("time_seconds"):
                s["all_times"].append(attempt["time_seconds"])
            history.append(
                {
                    "id": str(attempt["id"]),
                    "attempt_number": number,
                    "score_pct": score,
                    "marks_obtained": attempt["marks_obtained"],
                    "total_questions": attempt["total_questions"],
                    "time_seconds": attempt.get("time_seconds"),
                    "submitted_at": attempt.get("submitted_at"),
                    "status": "passed" if score >= PASS_MARK_PCT else "failed",
                }
            )
        if history:
            s["attempted_exams"] += 1
            s["total_attempts_count"] += len(history)
        best = max((h["score_pct"] for h in history), default=0)
        s["best_score_pct"] = max(s["best_score_pct"], best)
        s["exams"].append(
            {
                "id": str(exam["id"]),
                "title": exam["title"],
                "order_number": exam.get("order_number") or 0,
                "best_score_pct": best,
                "attempts_count": len(history),
                "latest_score_pct": history[-1]["score_pct"] if history else 0,
                "history": history[::-1],  # newest first
            }
        )

    all_scores, all_times = [], []
    total_unique = attempted_unique = total_attempts = 0
    subject_list = []
    for s in subjects.values():
        scores, times = s.pop("all_scores"), s.pop("all_times")
        s["avg_score_pct"] = _avg(scores)
        s["avg_time_seconds"] = _avg(times)
        s["score_trend"] = scores[-10:]
        s["time_trend"] = times[-10:]
        s["exams"].sort(key=lambda e: e["order_number"])
        all_scores.extend(scores)
        all_times.extend(times)
        total_unique += s["total_exams"]
        attempted_unique += s["attempted_exams"]
        total_attempts += s["total_attempts_count"]
        subject_list.append(s)

    return {
        "student": {
            "full_name": profile.get("full_name") or "",
            "grade": profile.get("grade"),
            "school": profile.get("school"),
            "district": profile.get("district"),
            "badges_count": badges,
            "phone_number": profile.get("mobile_no"),
            "parent_phone_number": None,  # Edura profiles don't record one
        },
        "overall": {
            "total_unique_exams": total_unique,
            "unique_exams_attempted": attempted_unique,
            "total_attempts_made": total_attempts,
            "completion_pct": (
                round(attempted_unique / total_unique * 100) if total_unique else 0
            ),
            "avg_score_pct": _avg(all_scores),
            "avg_time_seconds": _avg(all_times),
        },
        "subjects": sorted(subject_list, key=lambda s: s["subject"]),
    }


async def _exams_for(user_id: int) -> list[dict]:
    access = await clients.accessible(user_id)
    return await clients.exam_progress(
        user_id, access["course_ids"], access["lesson_ids"]
    )


async def _progress_for(user_id: int, profile: dict) -> dict:
    exams, badges = await asyncio.gather(
        _exams_for(user_id), clients.badge_count(user_id)
    )
    titles = await clients.course_titles({e["course_id"] for e in exams})
    return build_student_progress(profile, exams, titles, badges)


async def student_progress(user_id: int) -> dict | None:
    """None when the user doesn't exist or isn't a student."""
    profile = await clients.profile(user_id)
    if not profile or str(profile.get("role", "")).lower() != "student":
        return None
    return await _progress_for(user_id, profile)


async def progress_summary(concurrency: int = 8) -> list[dict]:
    """Completion and average score for every student (admin table)."""
    roster = await clients.students()
    gate = asyncio.Semaphore(concurrency)

    async def one(row: dict) -> dict:
        user_id = int(row["user_id"])
        async with gate:
            # The table only shows overall figures, so badges and titles aren't fetched.
            data = build_student_progress(row, await _exams_for(user_id), {}, badges=0)
        return {
            "user_id": str(user_id),
            "full_name": row.get("full_name") or "",
            "completion_pct": data["overall"]["completion_pct"],
            "avg_score_pct": data["overall"]["avg_score_pct"],
        }

    return list(await asyncio.gather(*(one(r) for r in roster)))


def _rate(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def _performance(
    item_id, title: str, kind: str, scores: list[float], failures: int, students: set
) -> dict:
    return {
        "id": str(item_id),
        "title": title,
        "type": kind,
        "avg_score": round(sum(scores) / len(scores), 1),
        "failure_rate": _rate(failures, len(scores)),
        "total_students": len(students),
        "attempt_count": len(scores),
    }


def build_course_struggle(course_id: int, modules: list[dict], data: dict) -> dict:
    """Where students struggle in a course, judged on FIRST attempts only (retakes would hide it)."""
    stats = {
        e["id"]: {
            "title": e["title"],
            "module_id": e.get("module_id"),
            "scores": [],
            "failures": 0,
            "students": set(),
        }
        for e in data["exams"]
    }
    scores_all = []
    for attempt in data["attempts"]:
        stat = stats.get(attempt["exam_id"])
        if stat is None:
            continue
        score = float(attempt["score_pct"])
        stat["scores"].append(score)
        stat["students"].add(attempt["student_id"])
        if score < FAIL_MARK_PCT:
            stat["failures"] += 1
        scores_all.append(score)

    exams = [
        _performance(eid, s["title"], "exam", s["scores"], s["failures"], s["students"])
        for eid, s in stats.items()
        if s["scores"]
    ]

    lessons = []
    for module in modules:
        in_lesson = [s for s in stats.values() if s["module_id"] == module["id"]]
        scores = [x for s in in_lesson for x in s["scores"]]
        if scores:
            lessons.append(
                _performance(
                    module["id"],
                    module["title"],
                    "lesson",
                    scores,
                    sum(s["failures"] for s in in_lesson),
                    set().union(*(s["students"] for s in in_lesson)),
                )
            )

    def hardest_first(item: dict) -> tuple:
        return (-item["failure_rate"], item["avg_score"])

    return {
        "course_id": str(course_id),
        "overall_avg_score": (
            round(sum(scores_all) / len(scores_all), 1) if scores_all else 0.0
        ),
        "overall_failure_rate": _rate(
            sum(1 for s in scores_all if s < FAIL_MARK_PCT), len(scores_all)
        ),
        "struggling_modules": sorted(lessons, key=hardest_first),
        "hardest_exams": sorted(exams, key=hardest_first),
    }


async def course_struggle(course_id: int) -> dict | None:
    """None when the course doesn't exist."""
    modules = await clients.course_modules(course_id)
    if modules is None:
        return None
    return build_course_struggle(
        course_id, modules, await clients.first_attempts(course_id)
    )
