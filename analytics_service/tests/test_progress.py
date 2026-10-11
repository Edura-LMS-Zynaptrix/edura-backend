"""Progress, struggle and report views. No database: every source service is stubbed."""

import base64

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import router as router_module
import service_clients
from progress import build_course_struggle, build_student_progress
from report import generate_report_pdf
from shared.auth import ALGORITHM, SECRET_KEY

PROFILE = {"user_id": 7, "role": "student", "full_name": "Nimal & Co", "grade": 11,
           "school": "Royal College", "district": "Colombo", "mobile_no": "0771234567"}


def _attempt(id, score, when, seconds=600):
    return {"id": id, "score_pct": score, "marks_obtained": score // 10, "total_questions": 10,
            "time_seconds": seconds, "submitted_at": when}


EXAMS = [
    {"id": 1, "title": "Algebra 1", "course_id": 10, "module_id": 100, "order_number": 2,
     "attempts": [_attempt(11, 40, "2026-10-01T10:00:00"), _attempt(12, 90, "2026-10-03T10:00:00")]},
    {"id": 2, "title": "Algebra 0", "course_id": 10, "module_id": 100, "order_number": 1, "attempts": []},
    {"id": 3, "title": "Cells", "course_id": 20, "module_id": None, "order_number": 1,
     "attempts": [_attempt(31, 70, "2026-10-02T10:00:00", seconds=300)]},
]
TITLES = {10: "Mathematics", 20: "Biology"}


def test_student_progress_groups_by_course_and_orders_history():
    data = build_student_progress(PROFILE, EXAMS, TITLES, badges=3)

    assert data["student"]["full_name"] == "Nimal & Co"
    assert data["student"]["badges_count"] == 3
    assert data["student"]["phone_number"] == "0771234567"
    assert data["overall"] == {
        "total_unique_exams": 3,
        "unique_exams_attempted": 2,
        "total_attempts_made": 3,
        "completion_pct": 67,
        "avg_score_pct": 67,  # (40 + 90 + 70) / 3
        "avg_time_seconds": 500,
    }
    biology, maths = data["subjects"]  # sorted by subject name
    assert (biology["subject"], maths["subject"]) == ("Biology", "Mathematics")
    assert maths["total_exams"] == 2 and maths["attempted_exams"] == 1
    assert maths["best_score_pct"] == 90 and maths["avg_score_pct"] == 65
    assert maths["score_trend"] == [40, 90]
    assert [e["title"] for e in maths["exams"]] == ["Algebra 0", "Algebra 1"]  # by order_number
    algebra = maths["exams"][1]
    assert algebra["latest_score_pct"] == 90
    assert [h["attempt_number"] for h in algebra["history"]] == [2, 1]  # newest first
    assert [h["status"] for h in algebra["history"]] == ["passed", "failed"]


def test_student_progress_with_nothing_available():
    data = build_student_progress(PROFILE, [], {}, badges=0)
    assert data["subjects"] == []
    assert data["overall"]["completion_pct"] == 0 and data["overall"]["avg_score_pct"] == 0


def test_course_struggle_uses_first_attempts_and_ranks_hardest_first():
    modules = [{"id": 100, "title": "Lesson A", "order": 1}, {"id": 200, "title": "Lesson B", "order": 2},
               {"id": 300, "title": "Empty lesson", "order": 3}]
    data = {
        "exams": [{"id": 1, "title": "Easy", "module_id": 100}, {"id": 2, "title": "Hard", "module_id": 200},
                  {"id": 3, "title": "Unattempted", "module_id": 100}],
        "attempts": [
            {"student_id": 1, "exam_id": 1, "score_pct": 80.0},
            {"student_id": 2, "exam_id": 1, "score_pct": 60.0},
            {"student_id": 1, "exam_id": 2, "score_pct": 20.0},
            {"student_id": 2, "exam_id": 2, "score_pct": 50.0},
            {"student_id": 3, "exam_id": 99, "score_pct": 0.0},  # exam outside the course: ignored
        ],
    }
    result = build_course_struggle(5, modules, data)

    assert result["course_id"] == "5"
    assert result["overall_avg_score"] == 52.5
    assert result["overall_failure_rate"] == 25.0
    assert [e["title"] for e in result["hardest_exams"]] == ["Hard", "Easy"]
    hard = result["hardest_exams"][0]
    assert hard == {"id": "2", "title": "Hard", "type": "exam", "avg_score": 35.0, "failure_rate": 50.0,
                    "total_students": 2, "attempt_count": 2}
    assert [m["title"] for m in result["struggling_modules"]] == ["Lesson B", "Lesson A"]
    assert result["struggling_modules"][0]["type"] == "lesson"


def test_report_pdf_renders_with_markup_characters_in_names():
    pdf = generate_report_pdf(build_student_progress(PROFILE, EXAMS, TITLES, badges=1))
    assert pdf.startswith(b"%PDF")
    assert generate_report_pdf(build_student_progress(PROFILE, [], {}, badges=0)).startswith(b"%PDF")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _token(role: str, sub: int) -> dict:
    return {"Authorization": "Bearer " + jwt.encode({"sub": str(sub), "role": role}, SECRET_KEY, algorithm=ALGORITHM)}


ADMIN = _token("ADMIN", 1)
STUDENT = _token("STUDENT", 7)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(router_module, "_redis", None)

    async def profile(user_id):
        return PROFILE if user_id == 7 else ({"role": "admin"} if user_id == 1 else None)

    async def accessible(user_id):
        return {"course_ids": [10, 20], "lesson_ids": []}

    async def exam_progress(user_id, course_ids, lesson_ids):
        return EXAMS

    async def badge_count(user_id):
        return 2

    async def course_titles(ids):
        return TITLES

    async def students(limit=500):
        return [{"user_id": "7", "full_name": "Nimal & Co"}]

    for name, fn in [("profile", profile), ("accessible", accessible), ("exam_progress", exam_progress),
                     ("badge_count", badge_count), ("course_titles", course_titles), ("students", students)]:
        monkeypatch.setattr(service_clients, name, fn)

    app = FastAPI()
    app.include_router(router_module.router)
    return TestClient(app)


def test_admin_reads_a_students_progress(client):
    r = client.get("/students/7/progress", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["overall"]["total_attempts_made"] == 3


def test_progress_is_admin_only(client):
    assert client.get("/students/7/progress", headers=STUDENT).status_code == 403
    assert client.get("/students/7/progress").status_code == 401
    assert client.get("/students/progress-summary", headers=STUDENT).status_code == 403
    assert client.post("/students/7/send-report", headers=STUDENT).status_code == 403
    assert client.get("/courses/5/struggle", headers=STUDENT).status_code == 403


def test_progress_404_for_unknown_or_non_student(client):
    assert client.get("/students/999/progress", headers=ADMIN).status_code == 404
    assert client.get("/students/1/progress", headers=ADMIN).status_code == 404


def test_student_reads_only_their_own_progress(client, monkeypatch):
    seen = []

    async def accessible(user_id):
        seen.append(user_id)
        return {"course_ids": [10], "lesson_ids": []}

    monkeypatch.setattr(service_clients, "accessible", accessible)
    r = client.get("/me/progress", headers=STUDENT)
    assert r.status_code == 200 and seen == [7]
    assert client.get("/me/progress", headers=ADMIN).status_code == 403


def test_summary(client):
    r = client.get("/students/progress-summary", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == [{"user_id": "7", "full_name": "Nimal & Co", "completion_pct": 67, "avg_score_pct": 67}]


def test_send_report_publishes_pdf_to_the_students_email(client, monkeypatch):
    published = []

    async def account(user_id):
        return {"id": user_id, "email": "nimal@example.com"}

    monkeypatch.setattr(router_module, "account", account)
    monkeypatch.setattr(router_module, "publish_event", lambda key, payload: published.append((key, payload)) or True)

    r = client.post("/students/7/send-report", headers=ADMIN)
    assert r.status_code == 200
    (key, payload), = published
    assert key == "report.progress"
    assert payload["email"] == "nimal@example.com" and payload["student_name"] == "Nimal & Co"
    assert payload["filename"].startswith("Progress_Report_Nimal_Co_") and payload["filename"].endswith(".pdf")
    assert base64.b64decode(payload["pdf_base64"]).startswith(b"%PDF")


def test_send_report_503_when_broker_is_down(client, monkeypatch):
    async def account(user_id):
        return {"id": user_id, "email": "nimal@example.com"}

    monkeypatch.setattr(router_module, "account", account)
    monkeypatch.setattr(router_module, "publish_event", lambda key, payload: False)
    assert client.post("/students/7/send-report", headers=ADMIN).status_code == 503


def test_source_outage_is_503_not_zeros(client, monkeypatch):
    async def down(*args, **kwargs):
        raise service_clients.SourceUnavailable("enrollment")

    monkeypatch.setattr(service_clients, "accessible", down)
    r = client.get("/students/7/progress", headers=ADMIN)
    assert r.status_code == 503 and r.json()["detail"]["error"] == "SOURCE_UNAVAILABLE"


def test_struggle_route(client, monkeypatch):
    async def modules(course_id):
        return None if course_id == 404 else [{"id": 100, "title": "Lesson A", "order": 1}]

    async def first(course_id):
        return {"exams": [{"id": 1, "title": "Quiz", "module_id": 100}],
                "attempts": [{"student_id": 1, "exam_id": 1, "score_pct": 30.0}]}

    monkeypatch.setattr(service_clients, "course_modules", modules)
    monkeypatch.setattr(service_clients, "first_attempts", first)
    r = client.get("/courses/5/struggle", headers=ADMIN)
    assert r.status_code == 200 and r.json()["overall_failure_rate"] == 100.0
    assert client.get("/courses/404/struggle", headers=ADMIN).status_code == 404



# ---------------------------------------------------------------------------
# Admin dashboard and analytics page
# ---------------------------------------------------------------------------

import dashboard  # noqa: E402

SOURCES = {
    "users": {
        "total_students": 3,
        "by_grade": [{"grade": 10, "count": 1}, {"grade": 11, "count": 2}],
        "by_district": [{"district": "Colombo", "count": 2}],
        "recent_students": [
            {"user_id": 7, "full_name": "Nimal", "district": "Colombo", "created_at": "2026-10-05T10:00:00+00:00"},
            {"user_id": 8, "full_name": "Kamal", "district": None, "created_at": "2026-10-01T10:00:00"},
        ],
    },
    "courses": [{"id": 10, "title": "Mathematics", "grade": 11}],
    "enrollments": [{"course_id": 10, "count": 4}, {"course_id": 20, "count": 2}, {"course_id": 99, "count": 5}],
    "assessments": {
        "published_exams": 2,
        "graded_attempts": 9,
        "attempts_by_month": [{"year": 2026, "month": 10, "count": 9}],
        "top_exams": [{"exam_id": 1, "title": "Algebra", "course_id": 20, "attempt_count": 9, "avg_score": 61.5}],
        "recent_exams": [{"title": "Algebra", "course_id": 20, "created_at": "2026-10-03T08:00:00+05:30"}],
    },
    "payments": {
        "total_revenue": 5000.0,
        "revenue_this_month": 1500.0,
        "pending_bank_slips": 1,
        "revenue_by_month": [{"year": 2026, "month": 10, "revenue": 1500, "count": 1}],
        "recent_verified": [{"student_id": 7, "verified_at": "2026-10-04T09:00:00+00:00"}],
    },
}


def test_dashboard_stats_and_activity_newest_first():
    stats = dashboard.build_stats(SOURCES, {7: "Nimal"}, {20: "Biology"})
    assert {k: stats[k] for k in ("total_students", "total_courses", "total_exams", "total_exam_attempts",
                                  "total_revenue", "revenue_this_month", "pending_bank_slips")} == {
        "total_students": 3, "total_courses": 1, "total_exams": 2, "total_exam_attempts": 9,
        "total_revenue": 5000.0, "revenue_this_month": 1500.0, "pending_bank_slips": 1,
    }
    assert [(a["type"], a["subtitle"]) for a in stats["recent_activity"]] == [
        ("student", "Nimal joined from Colombo"),
        ("payment", "Bank slip verified for Nimal"),
        ("exam", "Algebra — Biology"),
        ("student", "Kamal joined"),  # naive timestamps are read as UTC
    ]


def test_analytics_names_courses_and_skips_deleted_ones():
    courses = {10: {"id": 10, "title": "Mathematics", "grade": 11}, 20: {"id": 20, "title": "Biology", "grade": 10}}
    data = dashboard.build_analytics(SOURCES, courses)
    assert data["top_exams"] == [{"exam_id": "1", "title": "Algebra", "subject": "Biology", "grade": 10,
                                  "attempt_count": 9, "avg_score": 61.5}]
    assert data["enrollments_by_subject"] == [{"subject": "Mathematics", "count": 4}, {"subject": "Biology", "count": 2}]
    assert data["students_by_grade"] == SOURCES["users"]["by_grade"]
    assert data["revenue_by_month"] == SOURCES["payments"]["revenue_by_month"]


def test_admin_routes(client, monkeypatch):
    async def sources():
        return SOURCES

    async def names(ids):
        return {7: "Nimal"}

    async def course_info(course_id):
        return {"id": 20, "title": "Biology", "grade": 10} if course_id == 20 else None

    monkeypatch.setattr(service_clients, "admin_sources", sources)
    monkeypatch.setattr(service_clients, "names", names)
    monkeypatch.setattr(service_clients, "course_info", course_info)

    assert client.get("/admin/stats", headers=STUDENT).status_code == 403
    assert client.get("/admin/analytics").status_code == 401
    stats = client.get("/admin/stats", headers=ADMIN)
    assert stats.status_code == 200 and stats.json()["recent_activity"][2]["subtitle"] == "Algebra — Biology"
    analytics = client.get("/admin/analytics", headers=ADMIN).json()
    assert [r["subject"] for r in analytics["enrollments_by_subject"]] == ["Mathematics", "Biology"]

    async def down():
        raise service_clients.SourceUnavailable("payment")

    monkeypatch.setattr(service_clients, "admin_sources", down)
    assert client.get("/admin/stats", headers=ADMIN).status_code == 503
