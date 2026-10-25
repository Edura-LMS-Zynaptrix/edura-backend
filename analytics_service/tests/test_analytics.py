"""Gaps not covered by test_progress / test_readmodel: app wiring, the Redis cache,
public platform stats, error paths of every route, consumer edge cases and the
read-model and dashboard helpers on empty or partial data."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone

import dashboard
import event_consumer
import jwt
import main
import progress
import pytest
import readmodel
import report
import router as router_module
from database import Base
from fastapi.testclient import TestClient
from models import (
    ReplicaAttempt,
    ReplicaBadge,
    ReplicaCourse,
    ReplicaEnrollment,
    ReplicaExam,
    ReplicaModule,
    ReplicaPayment,
    ReplicaReceipt,
    ReplicaUser,
)
from readmodel import SourceUnavailable
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import events as ev
from shared.auth import ALGORITHM, SECRET_KEY


def _token(role: str, sub: int) -> dict:
    return {
        "Authorization": "Bearer "
        + jwt.encode({"sub": str(sub), "role": role}, SECRET_KEY, algorithm=ALGORITHM)
    }


ADMIN = _token("admin", 1)
STUDENT = _token("student", 7)


def run(coro):
    return asyncio.run(coro)


class FakeRedis:
    """Just enough of redis.Redis for the router's cache: get and set with a TTL."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttl[key] = ex


class BrokenRedis:
    def get(self, key):
        raise ConnectionError("redis down")

    def set(self, key, value, ex=None):
        raise ConnectionError("redis down")


@pytest.fixture
def cache(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(router_module, "_redis", fake)
    return fake


@pytest.fixture
def client(cache):
    return TestClient(main.app)


@pytest.fixture
def session(monkeypatch):
    """An empty read model on SQLite, wired into the consumer and the read model."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(event_consumer, "SessionLocal", factory)
    monkeypatch.setattr(readmodel, "SessionLocal", factory)
    yield factory
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def db(session):
    s = session()
    try:
        yield s
    finally:
        s.close()


def _add(db, *rows):
    db.add_all(rows)
    db.commit()


def _student_view(full_name="Nimal Perera") -> dict:
    return progress.build_student_progress({"full_name": full_name}, [], {}, badges=0)


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "analytics_service"}


def test_startup_creates_tables_and_starts_consumer(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    calls = []
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("start"))

    main.on_startup()

    assert calls == ["start"]
    assert {"replica_attempts", "replica_payments", "replica_users"} <= set(
        inspect(engine).get_table_names()
    )
    engine.dispose()


def test_startup_still_starts_consumer_when_db_unavailable(monkeypatch):
    calls = []

    def broken_create_all(bind):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken_create_all)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("start"))

    main.on_startup()

    assert calls == ["start"]


def test_analytics_endpoint_is_admin_only(client):
    assert client.get("/analytics", headers=ADMIN).json() == {
        "message": "Welcome Admin"
    }
    assert client.get("/analytics", headers=STUDENT).status_code == 403
    assert client.get("/analytics").status_code == 401


# ---------------------------------------------------------------------------
# Redis cache helpers
# ---------------------------------------------------------------------------


def test_cache_round_trip(cache):
    router_module._cache_set({"a": 1}, "k", 42)
    assert router_module._cache_get("k") == {"a": 1}
    assert cache.ttl["k"] == 42


def test_cache_defaults_to_platform_stats_key_and_ttl(cache):
    router_module._cache_set({"total_students": 3})
    assert router_module._cache_get() == {"total_students": 3}
    assert cache.ttl["analytics:platform-stats"] == 300


def test_cache_miss_returns_none(cache):
    assert router_module._cache_get("missing") is None


def test_cache_without_redis_is_a_no_op(monkeypatch):
    monkeypatch.setattr(router_module, "_redis", None)
    router_module._cache_set({"a": 1}, "k")
    assert router_module._cache_get("k") is None


def test_cache_errors_are_swallowed(monkeypatch):
    monkeypatch.setattr(router_module, "_redis", BrokenRedis())
    router_module._cache_set({"a": 1}, "k")  # does not raise
    assert router_module._cache_get("k") is None


def test_routes_still_work_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(router_module, "_redis", BrokenRedis())

    async def summary():
        return [{"user_id": "7"}]

    monkeypatch.setattr(progress, "progress_summary", summary)
    resp = TestClient(main.app).get("/students/progress-summary", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json() == [{"user_id": "7"}]


# ---------------------------------------------------------------------------
# Public platform stats
# ---------------------------------------------------------------------------

PLATFORM_SOURCES = {
    "users": {"total_students": 42},
    "courses": [
        {
            "id": 1,
            "title": "Maths",
            "grade": 11,
            "thumbnail_url": "m.png",
            "price": 1500,
            "course_type": "exam",
            "stream_ids": [2, 3],
        },
        {"id": 2, "title": "Biology"},
        {"id": 3, "title": "Physics"},
        {"id": 4, "title": "Chemistry"},
        {"id": 5, "title": "ICT"},
    ],
    "enrollments": [
        {"course_id": 1, "count": 9},
        {"course_id": 3, "count": 5},
        {"course_id": 4, "count": 7},
        {"course_id": 5, "count": 1},
    ],
    "assessments": {"published_exams": 6, "graded_attempts": 30},
}


def test_platform_stats_is_public_and_ranks_top_four(client, cache, monkeypatch):
    async def sources():
        return PLATFORM_SOURCES

    monkeypatch.setattr(router_module, "fetch_platform_sources", sources)

    resp = client.get("/platform-stats")
    assert resp.status_code == 200
    data = resp.json()
    assert (
        data["total_students"],
        data["total_courses"],
        data["total_exams"],
        data["total_attempts"],
    ) == (42, 5, 6, 30)
    # Most enrolled first; course 2 has no enrollments and drops off the top four.
    assert [c["id"] for c in data["top_courses"]] == ["1", "4", "3", "5"]
    assert data["top_courses"][0] == {
        "id": "1",
        "title": "Maths",
        "subject": "",
        "grade": 11,
        "image_url": "m.png",
        "price": 1500.0,
        "course_type": "exam",
        "enrollment_count": 9,
        "stream_ids": ["2", "3"],
    }
    # Missing fields fall back to safe defaults.
    chemistry = data["top_courses"][1]
    assert (chemistry["grade"], chemistry["price"], chemistry["course_type"]) == (
        0,
        0.0,
        "video",
    )
    assert chemistry["image_url"] is None and chemistry["stream_ids"] == []

    assert cache.ttl["analytics:platform-stats"] == 300


def test_platform_stats_with_empty_sources(client, monkeypatch):
    async def sources():
        return {"users": {}, "courses": [], "enrollments": [], "assessments": {}}

    monkeypatch.setattr(router_module, "fetch_platform_sources", sources)

    assert client.get("/platform-stats").json() == {
        "total_students": 0,
        "total_courses": 0,
        "total_exams": 0,
        "total_attempts": 0,
        "top_courses": [],
    }


def test_platform_stats_served_from_cache(client, monkeypatch):
    router_module._cache_set({"total_students": 99})

    async def sources():
        raise AssertionError("should use the cache")

    monkeypatch.setattr(router_module, "fetch_platform_sources", sources)
    assert client.get("/platform-stats").json() == {"total_students": 99}


def test_platform_stats_from_the_read_model(client, db):
    _add(
        db,
        ReplicaUser(user_id=7, role="student", full_name="Nimal"),
        ReplicaUser(user_id=8, role="student"),  # no profile yet: not counted
        ReplicaCourse(id=10, title="Maths", status="PUBLISHED", price=1000),
        ReplicaCourse(id=11, title="Draft", status="DRAFT"),
        ReplicaEnrollment(student_id=7, course_id=10, status="ACTIVE"),
        ReplicaExam(id=1, course_id=10, is_published=True),
        ReplicaAttempt(submission_id=1, student_id=7, exam_id=1),
    )

    data = client.get("/platform-stats").json()
    assert data["total_students"] == 1
    assert data["total_courses"] == 1
    assert data["top_courses"][0]["enrollment_count"] == 1


# ---------------------------------------------------------------------------
# Student progress routes — error paths and caching
# ---------------------------------------------------------------------------


def test_summary_is_cached_for_a_minute(client, cache, monkeypatch):
    calls = []

    async def summary():
        calls.append(1)
        return [{"user_id": "7"}]

    monkeypatch.setattr(progress, "progress_summary", summary)

    assert client.get("/students/progress-summary", headers=ADMIN).status_code == 200
    assert client.get("/students/progress-summary", headers=ADMIN).json() == [
        {"user_id": "7"}
    ]
    assert len(calls) == 1
    assert cache.ttl["analytics:progress-summary"] == 60


def test_empty_cached_summary_is_still_a_hit(client, monkeypatch):
    router_module._cache_set([], "analytics:progress-summary")

    async def summary():
        raise AssertionError("should use the cache")

    monkeypatch.setattr(progress, "progress_summary", summary)
    assert client.get("/students/progress-summary", headers=ADMIN).json() == []


def test_summary_503_when_read_model_down(client, cache, monkeypatch):
    async def summary():
        raise SourceUnavailable("db")

    monkeypatch.setattr(progress, "progress_summary", summary)
    resp = client.get("/students/progress-summary", headers=ADMIN)
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "SOURCE_UNAVAILABLE"
    assert "analytics:progress-summary" not in cache.store


def test_student_progress_404(client, monkeypatch):
    async def student_progress(user_id):
        return None

    monkeypatch.setattr(progress, "student_progress", student_progress)
    resp = client.get("/students/5/progress", headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "STUDENT_NOT_FOUND"


def test_me_progress_404_when_profile_missing(client, monkeypatch):
    async def student_progress(user_id):
        return None

    monkeypatch.setattr(progress, "student_progress", student_progress)
    assert client.get("/me/progress", headers=STUDENT).status_code == 404


def test_me_progress_requires_sign_in(client):
    assert client.get("/me/progress").status_code == 401


@pytest.fixture
def report_route(monkeypatch):
    """send-report with a student who exists; returns the list of published events."""
    published = []

    async def student_progress(user_id):
        return _student_view()

    async def account(user_id):
        return {"id": user_id, "email": "nimal@example.com"}

    monkeypatch.setattr(progress, "student_progress", student_progress)
    monkeypatch.setattr(router_module, "account", account)
    monkeypatch.setattr(
        router_module,
        "publish_event",
        lambda key, payload: published.append((key, payload)) or True,
    )
    return published


def test_send_report_404_for_unknown_student(client, report_route, monkeypatch):
    async def student_progress(user_id):
        return None

    monkeypatch.setattr(progress, "student_progress", student_progress)
    assert client.post("/students/5/send-report", headers=ADMIN).status_code == 404
    assert report_route == []


@pytest.mark.parametrize("acct", [None, {"id": 7, "email": None}, {"id": 7}])
def test_send_report_404_without_email(client, report_route, monkeypatch, acct):
    async def account(user_id):
        return acct

    monkeypatch.setattr(router_module, "account", account)
    resp = client.post("/students/7/send-report", headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "STUDENT_NOT_FOUND"
    assert report_route == []


def test_send_report_503_when_account_lookup_fails(client, report_route, monkeypatch):
    async def account(user_id):
        raise SourceUnavailable("db")

    monkeypatch.setattr(router_module, "account", account)
    resp = client.post("/students/7/send-report", headers=ADMIN)
    assert resp.status_code == 503
    assert report_route == []


def test_send_report_payload(client, report_route, monkeypatch):
    monkeypatch.setattr(
        router_module,
        "now_local",
        lambda: datetime(2026, 10, 25, tzinfo=report.SL_TIME),
    )

    resp = client.post("/students/7/send-report", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json() == {"message": "Progress report sent to nimal@example.com"}
    ((key, payload),) = report_route
    assert key == "report.progress"
    assert payload["user_id"] == 7
    assert payload["month"] == "October 2026"
    assert payload["filename"] == "Progress_Report_Nimal_Perera_20261025.pdf"
    assert base64.b64decode(payload["pdf_base64"]).startswith(b"%PDF")


def test_send_report_names_unnamed_student(client, report_route, monkeypatch):
    async def student_progress(user_id):
        return _student_view(full_name="")

    monkeypatch.setattr(progress, "student_progress", student_progress)

    assert client.post("/students/7/send-report", headers=ADMIN).status_code == 200
    payload = report_route[0][1]
    assert payload["student_name"] == "Student"
    assert payload["filename"].startswith("Progress_Report_Student_")


def test_send_report_requires_admin(client, report_route):
    assert client.post("/students/7/send-report").status_code == 401
    assert client.post("/students/7/send-report", headers=STUDENT).status_code == 403


def test_struggle_503_when_read_model_down(client, monkeypatch):
    async def course_struggle(course_id):
        raise SourceUnavailable("db")

    monkeypatch.setattr(progress, "course_struggle", course_struggle)
    assert client.get("/courses/5/struggle", headers=ADMIN).status_code == 503


def test_struggle_404_body(client, monkeypatch):
    async def course_struggle(course_id):
        return None

    monkeypatch.setattr(progress, "course_struggle", course_struggle)
    resp = client.get("/courses/5/struggle", headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "COURSE_NOT_FOUND"


def test_struggle_requires_sign_in(client):
    assert client.get("/courses/5/struggle").status_code == 401


# ---------------------------------------------------------------------------
# Admin dashboard routes — caching and errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, key, builder",
    [
        ("/admin/stats", "analytics:admin-stats", "admin_stats"),
        ("/admin/analytics", "analytics:admin-analytics", "admin_analytics"),
    ],
)
def test_admin_views_are_cached_for_thirty_seconds(
    client, cache, monkeypatch, url, key, builder
):
    calls = []

    async def build():
        calls.append(1)
        return {"view": builder}

    monkeypatch.setattr(dashboard, builder, build)

    assert client.get(url, headers=ADMIN).json() == {"view": builder}
    assert client.get(url, headers=ADMIN).json() == {"view": builder}
    assert len(calls) == 1
    assert cache.ttl[key] == 30


def test_admin_analytics_503_when_read_model_down(client, cache, monkeypatch):
    async def build():
        raise SourceUnavailable("db")

    monkeypatch.setattr(dashboard, "admin_analytics", build)
    assert client.get("/admin/analytics", headers=ADMIN).status_code == 503
    assert "analytics:admin-analytics" not in cache.store


def test_admin_views_require_admin(client):
    for url in ("/admin/stats", "/admin/analytics"):
        assert client.get(url).status_code == 401
        assert client.get(url, headers=STUDENT).status_code == 403


# ---------------------------------------------------------------------------
# dashboard helpers
# ---------------------------------------------------------------------------


def test_when_handles_missing_naive_and_aware_timestamps():
    assert dashboard._when(None) == datetime.min.replace(tzinfo=timezone.utc)
    assert dashboard._when("") == datetime.min.replace(tzinfo=timezone.utc)
    assert dashboard._when("2026-10-01T10:00:00") == datetime(
        2026, 10, 1, 10, tzinfo=timezone.utc
    )
    aware = dashboard._when("2026-10-01T10:00:00+05:30")
    assert aware.utcoffset() == timedelta(hours=5, minutes=30)


def _empty_sources(**overrides) -> dict:
    src = {
        "users": {},
        "courses": [],
        "enrollments": [],
        "assessments": {},
        "payments": {},
    }
    src.update(overrides)
    return src


def test_build_stats_with_no_data():
    assert dashboard.build_stats(_empty_sources(), {}, {}) == {
        "total_students": 0,
        "total_courses": 0,
        "total_exams": 0,
        "total_exam_attempts": 0,
        "total_revenue": 0,
        "revenue_this_month": 0,
        "pending_bank_slips": 0,
        "recent_activity": [],
    }


def test_build_stats_fallback_labels():
    src = _empty_sources(
        users={"recent_students": [{"full_name": None, "created_at": None}]},
        payments={"recent_verified": [{"student_id": 99, "verified_at": None}]},
        assessments={
            "recent_exams": [{"title": "Quiz", "course_id": 5, "created_at": None}]
        },
    )
    subtitles = {
        a["type"]: a["subtitle"]
        for a in dashboard.build_stats(src, {}, {})["recent_activity"]
    }
    assert subtitles == {
        "student": "A student joined",
        "payment": "Bank slip verified for Unknown",
        "exam": "Quiz",  # course title unknown: exam title alone
    }


def test_build_stats_keeps_eight_newest_activities():
    students = [
        {"full_name": f"S{i}", "created_at": f"2026-10-{i + 1:02d}T00:00:00+00:00"}
        for i in range(10)
    ]
    activity = dashboard.build_stats(
        _empty_sources(users={"recent_students": students}), {}, {}
    )["recent_activity"]
    assert len(activity) == 8
    assert activity[0]["subtitle"] == "S9 joined"
    assert activity[-1]["subtitle"] == "S2 joined"


def test_build_analytics_untitled_courses_and_ordering():
    src = _empty_sources(
        enrollments=[
            {"course_id": 1, "count": 3},
            {"course_id": 2, "count": 3},
            {"course_id": 3, "count": 0},
        ],
        assessments={
            "top_exams": [
                {"exam_id": 9, "title": "Q", "course_id": 1, "attempt_count": 1}
                | {"avg_score": 50.0}
            ]
        },
    )
    courses = {1: {"title": None}, 2: {"title": "Art"}, 3: {"title": "Empty"}}
    data = dashboard.build_analytics(src, courses)

    assert data["top_exams"][0]["subject"] == "Untitled course"
    assert data["top_exams"][0]["grade"] is None
    # Ties by count are ordered by name; zero-count courses are dropped.
    assert data["enrollments_by_subject"] == [
        {"subject": "Art", "count": 3},
        {"subject": "Untitled course", "count": 3},
    ]
    assert data["revenue_by_month"] == [] and data["students_by_grade"] == []


def test_courses_for_looks_up_only_unpublished_courses(monkeypatch):
    looked_up = []

    async def course_info(course_id):
        looked_up.append(course_id)
        return {"id": 20, "title": "Draft"} if course_id == 20 else None

    monkeypatch.setattr(readmodel, "course_info", course_info)
    src = _empty_sources(courses=[{"id": 10, "title": "Published"}])

    known = run(dashboard._courses_for(src, {10, 20, 30}))

    assert sorted(looked_up) == [20, 30]
    assert set(known) == {10, 20}  # 30 no longer exists


# ---------------------------------------------------------------------------
# readmodel queries
# ---------------------------------------------------------------------------


def test_run_wraps_query_errors_as_source_unavailable(session):
    def broken(db):
        raise RuntimeError("connection refused")

    with pytest.raises(SourceUnavailable, match="connection refused"):
        run(readmodel._run(broken))


def test_local_time_treats_naive_as_utc():
    local = readmodel._local(datetime(2026, 1, 1, 0, 0))
    assert (local.hour, local.minute) == (5, 30)


def test_account(db):
    _add(
        db,
        ReplicaUser(user_id=7, email="n@example.com"),
        ReplicaUser(user_id=8),
    )
    assert run(readmodel.account(7)) == {"id": 7, "email": "n@example.com"}
    assert run(readmodel.account(8)) is None  # no email
    assert run(readmodel.account(9)) is None  # unknown


def test_profile_needs_a_name(db):
    _add(
        db,
        ReplicaUser(user_id=7, role="student", full_name="Nimal", grade=11),
        ReplicaUser(user_id=8, role="student", email="k@example.com"),
    )
    assert run(readmodel.profile(7))["full_name"] == "Nimal"
    assert run(readmodel.profile(8)) is None
    assert run(readmodel.profile(9)) is None


def test_empty_lookups_skip_the_database(monkeypatch):
    def no_db():
        raise AssertionError("should not open a session")

    monkeypatch.setattr(readmodel, "SessionLocal", no_db)
    assert run(readmodel.names([])) == {}
    assert run(readmodel.course_titles([])) == {}
    assert run(readmodel.exam_progress(7, [], [])) == []


def test_names_and_course_titles(db):
    _add(
        db,
        ReplicaUser(user_id=7, full_name="Nimal"),
        ReplicaUser(user_id=8, email="k@example.com"),
        ReplicaUser(user_id=9),
        ReplicaCourse(id=10, title="Maths"),
    )
    assert run(readmodel.names([7, 8, 9, 10])) == {7: "Nimal", 8: "k@example.com"}
    assert run(readmodel.course_titles([10, 11])) == {10: "Maths"}


def test_students_lists_named_students_by_name(db):
    _add(
        db,
        ReplicaUser(user_id=1, role="student", full_name="Zara"),
        ReplicaUser(user_id=2, role="student", full_name="Amal"),
        ReplicaUser(user_id=3, role="student"),
        ReplicaUser(user_id=4, role="admin", full_name="Admin"),
    )
    assert [s["full_name"] for s in run(readmodel.students())] == ["Amal", "Zara"]
    assert len(run(readmodel.students(limit=1))) == 1
    assert run(readmodel.students())[0]["user_id"] == "2"


def test_course_info_defaults(db):
    _add(db, ReplicaCourse(id=10, title="Maths"))
    info = run(readmodel.course_info(10))
    assert info["course_type"] == "video"
    assert info["price"] == 0.0
    assert info["stream_ids"] == []
    assert run(readmodel.course_info(11)) is None


def test_course_modules(db):
    _add(
        db,
        ReplicaCourse(id=10, title="Maths"),
        ReplicaModule(id=2, course_id=10, title="Second", position=2),
        ReplicaModule(id=1, course_id=10, title="First", position=1),
        ReplicaModule(id=3, course_id=11, title="Other course", position=1),
    )
    assert run(readmodel.course_modules(10)) == [
        {"id": 1, "title": "First", "order": 1},
        {"id": 2, "title": "Second", "order": 2},
    ]
    assert run(readmodel.course_modules(99)) is None
    _add(db, ReplicaCourse(id=12, title="Empty"))
    assert run(readmodel.course_modules(12)) == []


def test_exam_progress_combines_courses_and_single_lessons(db):
    _add(
        db,
        ReplicaExam(id=1, course_id=10, module_id=100, is_published=True),
        ReplicaExam(id=2, course_id=20, module_id=200, is_published=True),
        ReplicaExam(id=3, course_id=30, module_id=300, is_published=True),
        ReplicaExam(id=4, course_id=10, module_id=100, is_published=False),
    )
    ids = lambda rows: [e["id"] for e in rows]  # noqa: E731
    assert ids(run(readmodel.exam_progress(7, [10], [200]))) == [1, 2]
    assert ids(run(readmodel.exam_progress(7, [10], []))) == [1]
    assert ids(run(readmodel.exam_progress(7, [], [300]))) == [3]
    assert run(readmodel.exam_progress(7, [99], [])) == []


def test_exam_progress_lists_only_this_students_attempts(db):
    when = datetime(2026, 10, 1, tzinfo=timezone.utc)
    _add(
        db,
        ReplicaExam(id=1, course_id=10, is_published=True),
        ReplicaAttempt(
            submission_id=2, student_id=7, exam_id=1, score=80.4, submitted_at=when
        ),
        ReplicaAttempt(
            submission_id=1,
            student_id=7,
            exam_id=1,
            score=40,
            submitted_at=when - timedelta(days=1),
        ),
        ReplicaAttempt(submission_id=3, student_id=8, exam_id=1, score=100),
    )
    (exam,) = run(readmodel.exam_progress(7, [10], []))
    assert [a["id"] for a in exam["attempts"]] == [1, 2]  # oldest first
    assert exam["attempts"][1]["score_pct"] == 80


def test_first_attempts_for_course_without_exams(db):
    assert run(readmodel.first_attempts(10)) == {"exams": [], "attempts": []}


def test_badge_count(db):
    _add(
        db,
        ReplicaBadge(student_id=7, badge_key="first"),
        ReplicaBadge(student_id=7, badge_key="streak"),
        ReplicaBadge(student_id=8, badge_key="first"),
    )
    assert run(readmodel.badge_count(7)) == 2
    assert run(readmodel.badge_count(9)) == 0


def test_payments_without_completion_time_count_only_in_total(db):
    now = datetime.now(timezone.utc)
    _add(
        db,
        ReplicaPayment(order_id="a", student_id=7, amount=1000, completed_at=now),
        ReplicaPayment(order_id="b", student_id=7, amount=500, completed_at=None),
        ReplicaPayment(order_id="c", student_id=7, amount=0, completed_at=now),
        ReplicaReceipt(receipt_id=1, student_id=7, status="PENDING"),
        ReplicaReceipt(receipt_id=2, student_id=7, status="APPROVED"),  # no time
    )
    payments = readmodel._payments_for_admin(db)
    assert payments["total_revenue"] == 1500.0
    assert payments["revenue_this_month"] == 1000.0
    assert [m["count"] for m in payments["revenue_by_month"]] == [1]
    assert payments["pending_bank_slips"] == 1
    assert payments["recent_verified"] == []


def test_admin_sources_on_an_empty_read_model(session):
    src = run(readmodel.admin_sources())
    assert src["users"]["total_students"] == 0
    assert src["assessments"]["top_exams"] == []
    assert src["payments"]["revenue_by_month"] == []
    assert src["courses"] == [] and src["enrollments"] == []


# ---------------------------------------------------------------------------
# event_consumer edge cases
# ---------------------------------------------------------------------------


def test_enrollment_defaults_to_active_and_updates_in_place(session, db):
    send = event_consumer.handle
    send(ev.ENROLLMENT_CHANGED, {"student_id": 7, "course_id": 10})
    assert db.get(ReplicaEnrollment, (7, 10)).status == "ACTIVE"

    send(ev.ENROLLMENT_CHANGED, {"student_id": 7, "course_id": 10, "status": "revoked"})
    db.expire_all()
    assert db.query(ReplicaEnrollment).count() == 1
    assert db.get(ReplicaEnrollment, (7, 10)).status == "REVOKED"


def test_assessment_upsert_defaults_and_update(session, db):
    send = event_consumer.handle
    send(ev.ASSESSMENT_UPSERTED, {"id": 1, "course_id": 10})
    exam = db.get(ReplicaExam, 1)
    assert (exam.title, exam.position, exam.is_published, exam.created_at) == (
        "",
        0,
        False,
        None,
    )

    send(
        ev.ASSESSMENT_UPSERTED,
        {"id": 1, "course_id": 11, "title": "Quiz", "is_published": True},
    )
    db.expire_all()
    exam = db.get(ReplicaExam, 1)
    assert (exam.course_id, exam.title, exam.is_published) == (11, "Quiz", True)


def test_deleting_an_unknown_exam_is_harmless(session, db):
    event_consumer.handle(ev.ASSESSMENT_DELETED, {"id": 404})
    assert db.query(ReplicaExam).count() == 0


def test_graded_event_without_submission_id_is_ignored(session, db):
    event_consumer.handle(
        ev.ASSESSMENT_GRADED, {"student_id": 7, "assessment_id": 1, "score": 90}
    )
    assert db.query(ReplicaAttempt).count() == 0


def test_graded_event_defaults_and_regrade(session, db):
    send = event_consumer.handle
    send(
        ev.ASSESSMENT_GRADED, {"submission_id": 5, "student_id": 7, "assessment_id": 1}
    )
    attempt = db.get(ReplicaAttempt, 5)
    assert (attempt.score, attempt.passed, attempt.marks_obtained) == (0.0, False, 0)
    assert attempt.submitted_at is None

    send(
        ev.ASSESSMENT_GRADED,
        {
            "submission_id": 5,
            "student_id": 7,
            "assessment_id": 1,
            "score": "88.5",
            "passed": True,
        },
    )
    db.expire_all()
    attempt = db.get(ReplicaAttempt, 5)
    assert (attempt.score, attempt.passed) == (88.5, True)
    assert db.query(ReplicaAttempt).count() == 1


def test_badges_are_stored_once_and_need_a_key(session, db):
    send = event_consumer.handle
    send(ev.BADGE_AWARDED, {"student_id": 7, "badge_key": "first"})
    send(ev.BADGE_AWARDED, {"student_id": 7, "badge_key": "first"})
    send(ev.BADGE_AWARDED, {"student_id": 7, "badge_key": ""})
    assert db.query(ReplicaBadge).count() == 1


def test_payment_without_order_id_is_ignored(session, db):
    event_consumer.handle(ev.PAYMENT_SUCCESS, {"student_id": 7, "amount": 100})
    event_consumer.handle(
        ev.PAYMENT_SUCCESS, {"order_id": "", "student_id": 7, "amount": 100}
    )
    assert db.query(ReplicaPayment).count() == 0


def test_payment_defaults_completion_time_and_updates(session, db):
    send = event_consumer.handle
    before = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    send(ev.PAYMENT_SUCCESS, {"order_id": "o1", "student_id": 7})
    payment = db.get(ReplicaPayment, "o1")
    assert float(payment.amount) == 0.0
    assert payment.completed_at.replace(tzinfo=None) >= before

    send(
        ev.PAYMENT_SUCCESS,
        {
            "order_id": "o1",
            "student_id": 7,
            "amount": 2500,
            "completed_at": "2026-10-01T10:00:00Z",
        },
    )
    db.expire_all()
    payment = db.get(ReplicaPayment, "o1")
    assert float(payment.amount) == 2500.0
    assert payment.completed_at.replace(tzinfo=None) == datetime(2026, 10, 1, 10)
    assert db.query(ReplicaPayment).count() == 1


def test_receipt_upload_does_not_reset_a_reviewed_receipt(session, db):
    send = event_consumer.handle
    send(ev.PAYMENT_RECEIPT_UPLOADED, {"receipt_id": 1, "student_id": 7})
    assert db.get(ReplicaReceipt, 1).status == "PENDING"

    send(
        ev.PAYMENT_RECEIPT_REVIEWED,
        {"receipt_id": 1, "student_id": 7, "status": "approved"},
    )
    send(ev.PAYMENT_RECEIPT_UPLOADED, {"receipt_id": 1, "student_id": 7})  # replayed
    db.expire_all()
    assert db.get(ReplicaReceipt, 1).status == "APPROVED"


def test_review_before_upload_creates_the_receipt(session, db):
    event_consumer.handle(
        ev.PAYMENT_RECEIPT_REVIEWED,
        {
            "receipt_id": 9,
            "student_id": 7,
            "status": "rejected",
            "reviewed_at": "2026-10-02T08:00:00+00:00",
        },
    )
    receipt = db.get(ReplicaReceipt, 9)
    assert receipt.student_id == 7
    assert receipt.status == "REJECTED"
    assert receipt.reviewed_at.replace(tzinfo=None) == datetime(2026, 10, 2, 8)


def test_shared_events_go_to_the_replica_tables(session, db):
    event_consumer.handle(
        ev.AUTH_USER_REGISTERED,
        {"user_id": 7, "email": "n@example.com", "role": "student"},
    )
    event_consumer.handle("something.else", {"id": 1})  # unknown: ignored
    assert db.get(ReplicaUser, 7).email == "n@example.com"


def test_consumer_rolls_back_and_reraises(monkeypatch):
    class FakeSession:
        rolled_back = closed = committed = False

        def rollback(self):
            self.rolled_back = True

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

    fake = FakeSession()

    def explode(db, data):
        raise ValueError("bad event")

    monkeypatch.setattr(event_consumer, "SessionLocal", lambda: fake)
    monkeypatch.setitem(event_consumer._OWN, ev.BADGE_AWARDED, explode)

    with pytest.raises(ValueError):
        event_consumer.handle(ev.BADGE_AWARDED, {})
    assert fake.rolled_back and fake.closed and not fake.committed


def test_consumer_start_binds_analytics_queue(monkeypatch):
    calls = []
    monkeypatch.setattr(
        event_consumer.messaging, "start_consumer", lambda *args: calls.append(args)
    )

    event_consumer.start()

    assert calls == [
        (
            "analytics",
            "analytics.queue",
            ev.QUEUES["analytics.queue"],
            event_consumer.handle,
        )
    ]
    # Every event with its own handler is actually bound to the queue.
    assert set(event_consumer._OWN) <= set(ev.QUEUES["analytics.queue"])


# ---------------------------------------------------------------------------
# report helpers
# ---------------------------------------------------------------------------


def test_minutes_and_text_helpers():
    assert report._minutes(90) == "1.5 min"
    assert report._minutes(0) == "N/A"
    assert report._minutes(None) == "N/A"
    assert report._text("A & B") == "A &amp; B"
    assert report._text("") == "N/A"
    assert report._text(None) == "N/A"
    assert report._text(0) == "0"


def test_now_local_is_sri_lanka_time():
    assert report.now_local().utcoffset() == timedelta(hours=5, minutes=30)


def test_report_renders_subject_without_attempts_and_long_titles():
    exams = [
        {"id": 1, "title": "Unattempted", "course_id": 10, "attempts": []},
        {
            "id": 2,
            "title": "A very long exam title that will not fit in the table cell",
            "course_id": 20,
            "attempts": [
                {
                    "id": 1,
                    "score_pct": 70,
                    "marks_obtained": 7,
                    "total_questions": 10,
                    "time_seconds": None,
                    "submitted_at": None,
                }
            ],
        },
    ]
    data = progress.build_student_progress(
        {"full_name": "Nimal"}, exams, {10: "Maths", 20: "Biology"}, badges=0
    )
    assert report.generate_report_pdf(data).startswith(b"%PDF")
