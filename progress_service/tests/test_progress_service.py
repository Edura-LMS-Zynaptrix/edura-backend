"""Progress service gaps: lesson-progress routes, dashboard and leaderboard edge
cases, completion and certificate rules, the consumer's edge cases and reconnect
loop, the certificate event publisher, the in-memory Redis and the app lifespan.
RabbitMQ is never contacted."""

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import consumer
import events
import jwt
import main
import pytest
import services
from database import Base, get_db
from fastapi.testclient import TestClient
from main import app
from models import (
    Certificate,
    CourseProgress,
    CourseProgressStatus,
    LeaderboardEntry,
    LessonProgress,
)
from redis_client import MemoryRedisClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import auth

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


client = TestClient(app)


def _auth(user_id: int, role: str = "student") -> dict:
    token = jwt.encode(
        {"sub": str(user_id), "role": role}, auth.SECRET_KEY, algorithm=auth.ALGORITHM
    )
    return {"Authorization": f"Bearer {token}"}


STUDENT = _auth(101)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setitem(app.dependency_overrides, get_db, override_get_db)
    monkeypatch.setattr(consumer, "SessionLocal", TestingSessionLocal)
    # Fresh per-test state for the module-level stores.
    monkeypatch.setattr(services, "_assessment_results_store", {})
    store = MemoryRedisClient()
    monkeypatch.setattr(services, "get_redis_client", lambda: store)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def certificates(monkeypatch):
    issued = []
    monkeypatch.setattr(
        services, "publish_certificate_issued", lambda **kw: issued.append(kw)
    )
    return issued


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _watch(course_id=10, lesson_id=1, headers=STUDENT, **body):
    return client.post(
        f"/api/progress/courses/{course_id}/lessons/progress",
        json={"lesson_id": lesson_id, **body},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------


def test_health():
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "progress_service",
    }


def test_lifespan_creates_tables_and_starts_the_consumer_thread(monkeypatch):
    fresh = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    started = threading.Event()
    monkeypatch.setattr(main, "engine", fresh)
    monkeypatch.setattr(consumer, "start_consumer", started.set)

    with TestClient(app):
        assert started.wait(timeout=5)

    with fresh.connect() as conn:
        assert "course_progress" in fresh.dialect.get_table_names(conn)
    fresh.dispose()


def test_lifespan_survives_create_all_failure(monkeypatch):
    started = threading.Event()

    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    monkeypatch.setattr(consumer, "start_consumer", started.set)
    with TestClient(app):
        assert started.wait(timeout=5)


# ---------------------------------------------------------------------------
# Lesson progress routes
# ---------------------------------------------------------------------------


def test_mark_lesson_watched_and_list_progress():
    resp = _watch(
        lesson_id=3,
        total_lessons=4,
        watch_duration_seconds=120,
        last_position_seconds=118,
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "lesson_id": 3,
        "is_completed": True,
        "watch_duration_seconds": 120,
        "last_position_seconds": 118,
    }
    _watch(course_id=11, lesson_id=9)  # another course

    rows = client.get(
        "/api/progress/courses/10/lessons/progress", headers=STUDENT
    ).json()
    assert [r["lesson_id"] for r in rows] == [3]
    # The legacy un-prefixed paths serve the same data.
    assert client.get("/courses/10/lessons/progress", headers=STUDENT).json() == rows


def test_rewatching_keeps_the_longest_watch_time(db):
    _watch(lesson_id=1, watch_duration_seconds=300, last_position_seconds=300)
    resp = _watch(lesson_id=1, watch_duration_seconds=60, last_position_seconds=60)
    assert (
        resp.json()["watch_duration_seconds"],
        resp.json()["last_position_seconds"],
    ) == (
        300,
        60,
    )
    assert db.query(LessonProgress).count() == 1


def test_only_students_mark_lessons_watched():
    assert _watch(headers=_auth(5, "teacher")).status_code == 403
    assert _watch(headers={}).status_code == 401
    assert _watch(lesson_id="x").status_code == 422


def test_dashboard_with_no_progress():
    data = client.get("/api/progress/courses/10", headers=STUDENT).json()
    assert data == {
        "completion_percentage": 0.0,
        "watched_lessons": 0,
        "total_lessons": 0,
        "assessment_scores": [],
        "certificate_issued": False,
    }


def test_dashboard_rejects_a_token_without_a_user():
    resp = client.get("/courses/10", headers=_auth(0))
    assert resp.status_code == 401


def test_dashboard_after_activity(db):
    _watch(lesson_id=1, total_lessons=2)
    services.process_assessment_graded(db, 101, 10, 7, 88.0, True)
    data = client.get("/courses/10", headers=_auth(101, "teacher")).json()
    assert (
        data["completion_percentage"],
        data["watched_lessons"],
        data["total_lessons"],
    ) == (
        50.0,
        1,
        2,
    )
    assert data["assessment_scores"] == [
        {"assessment_id": 7, "score": 88.0, "passed": True}
    ]


def test_leaderboard_falls_back_to_the_database(db):
    db.add_all(
        [
            LeaderboardEntry(student_id=1, course_id=10, points=30),
            LeaderboardEntry(student_id=2, course_id=10, points=90),
            LeaderboardEntry(student_id=3, course_id=11, points=99),
        ]
    )
    db.commit()
    rows = client.get("/api/progress/courses/10/leaderboard", headers=STUDENT).json()
    assert rows == [
        {"rank": 1, "student_id": 2, "points": 90},
        {"rank": 2, "student_id": 1, "points": 30},
    ]


def test_leaderboard_prefers_redis(db):
    services.get_redis_client().zadd("leaderboard:course:10", {"5": 40, "6": 70})
    db.add(LeaderboardEntry(student_id=1, course_id=10, points=999))
    db.commit()
    rows = client.get("/courses/10/leaderboard", headers=STUDENT).json()
    assert [r["student_id"] for r in rows] == [6, 5]


def test_leaderboard_is_capped_at_ten(db):
    services.get_redis_client().zadd(
        "leaderboard:course:10", {str(i): i for i in range(15)}
    )
    rows = client.get("/courses/10/leaderboard", headers=STUDENT).json()
    assert len(rows) == 10 and rows[0]["student_id"] == 14


# ---------------------------------------------------------------------------
# Completion and certificates
# ---------------------------------------------------------------------------


def test_unknown_lesson_count_treats_one_lesson_as_the_whole_course(db, certificates):
    services.process_lesson_watched(db, 101, 10, 1)
    cp = db.query(CourseProgress).one()
    assert (float(cp.completion_percentage), cp.total_lessons_count) == (100.0, 1)
    assert cp.status == CourseProgressStatus.completed
    assert len(certificates) == 1


def test_more_lessons_drop_completion_but_keep_the_first_completion_date(db):
    services.process_lesson_watched(db, 101, 10, 1, total_lessons=1)
    first_done = db.query(CourseProgress).one().completed_at

    services.process_lesson_watched(db, 101, 10, 2, total_lessons=4)
    db.expire_all()
    cp = db.query(CourseProgress).one()
    assert (float(cp.completion_percentage), cp.status) == (
        50.0,
        CourseProgressStatus.in_progress,
    )
    assert cp.completed_at == first_done

    services.process_lesson_watched(db, 101, 10, 3)  # total stays 4
    db.expire_all()
    assert float(db.query(CourseProgress).one().completion_percentage) == 75.0


def test_a_failed_assessment_blocks_the_certificate(db, certificates):
    services.process_assessment_graded(db, 101, 10, 1, 30.0, False)
    services.process_lesson_watched(db, 101, 10, 1, total_lessons=1)
    assert certificates == [] and db.query(Certificate).count() == 0

    services.process_assessment_graded(db, 101, 10, 1, 90.0, True)  # retake passes
    assert len(certificates) == 1
    assert certificates[0]["certificate_id"] == db.query(Certificate).one().id


def test_certificate_is_issued_once(db, certificates):
    services.process_lesson_watched(db, 101, 10, 1, total_lessons=1)
    services.process_assessment_graded(db, 101, 10, 2, 100.0, True)
    assert db.query(Certificate).count() == 1 and len(certificates) == 1
    assert services.check_certificate_eligibility(db, 101, 10).certificate_url == (
        "/certificates/101_10.pdf"
    )


def test_points_are_scores_plus_a_completion_bonus(db):
    services.process_assessment_graded(db, 101, 10, 1, 75.6, True)
    assert services.calculate_total_points(db, 101, 10) == 75
    services.process_lesson_watched(db, 101, 10, 1, total_lessons=1)
    assert services.calculate_total_points(db, 101, 10) == 85
    entry = db.query(LeaderboardEntry).one()
    assert entry.points == 85
    assert services.get_redis_client().zscore("leaderboard:course:10", "101") == 85.0


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------


def _deliver(routing_key, body):
    ch = MagicMock()
    method = SimpleNamespace(routing_key=routing_key, delivery_tag=4)
    consumer.process_message(
        ch, method, None, body if isinstance(body, str) else json.dumps(body)
    )
    return ch


def test_incomplete_events_are_acked_without_changes(db):
    for key, body in [
        ("lesson.watched", {"student_id": 101, "course_id": 10}),
        ("assessment.graded", {"student_id": 101, "score": 50}),
        ("something.else", {"student_id": 101}),
    ]:
        _deliver(key, body).basic_ack.assert_called_once_with(delivery_tag=4)
    assert db.query(LessonProgress).count() == 0
    assert db.query(LeaderboardEntry).count() == 0


def test_graded_event_without_a_course_goes_to_course_1(db):
    _deliver(
        "assessment.graded", {"student_id": 101, "assessment_id": 5, "score": "60"}
    )
    assert services.get_assessment_results(101, 1) == [
        {"assessment_id": 5, "score": 60.0, "passed": False}
    ]


def test_lesson_watched_event(db):
    _deliver(
        "lesson.watched",
        {
            "student_id": 101,
            "course_id": 10,
            "lesson_id": 2,
            "total_lessons": 2,
            "watch_duration": 30,
        },
    )
    assert db.query(LessonProgress).one().watch_duration_seconds == 30


def test_bad_messages_are_dropped(db):
    _deliver("lesson.watched", "{not json").basic_nack.assert_called_once_with(
        delivery_tag=4, requeue=False
    )
    ch = _deliver(
        "assessment.graded", {"student_id": 1, "assessment_id": 1, "score": "x"}
    )
    ch.basic_nack.assert_called_once()


def test_consumer_reconnects_until_the_broker_answers(monkeypatch):
    channel = MagicMock()
    attempts = []

    def connect(params):
        attempts.append(params)
        if len(attempts) == 1:
            raise ConnectionError("not yet")
        conn = MagicMock()
        conn.channel.return_value = channel
        return conn

    sleeps = []
    monkeypatch.setattr(consumer.pika, "BlockingConnection", connect)
    monkeypatch.setattr(consumer.time, "sleep", sleeps.append)

    consumer.start_consumer()

    assert sleeps == [5]
    keys = [c.kwargs["routing_key"] for c in channel.queue_bind.call_args_list]
    assert keys == ["lesson.watched", "assessment.graded"]
    channel.start_consuming.assert_called_once()


# ---------------------------------------------------------------------------
# certificate.issued publisher
# ---------------------------------------------------------------------------


def test_certificate_event_is_published_durably(monkeypatch):
    channel = MagicMock()
    conn = MagicMock()
    conn.channel.return_value = channel
    monkeypatch.setattr(events.pika, "BlockingConnection", lambda params: conn)

    issued = datetime(2026, 10, 1, tzinfo=timezone.utc)
    events.publish_certificate_issued(101, 10, 3, issued)

    kwargs = channel.basic_publish.call_args.kwargs
    assert kwargs["routing_key"] == "certificate.issued"
    assert json.loads(kwargs["body"]) == {
        "student_id": 101,
        "course_id": 10,
        "certificate_id": 3,
        "issued_at": issued.isoformat(),
    }
    assert kwargs["properties"].delivery_mode == 2
    conn.close.assert_called_once()


def test_certificate_event_defaults_the_time_and_never_raises(monkeypatch):
    def down(params):
        raise ConnectionError("broker down")

    monkeypatch.setattr(events.pika, "BlockingConnection", down)
    events.publish_certificate_issued(101, 10, 3)  # logged, not raised

    channel = MagicMock()
    conn = MagicMock(channel=MagicMock(return_value=channel))
    monkeypatch.setattr(events.pika, "BlockingConnection", lambda params: conn)
    events.publish_certificate_issued(101, 10, 3)
    issued_at = json.loads(channel.basic_publish.call_args.kwargs["body"])["issued_at"]
    assert datetime.fromisoformat(issued_at).year >= 2026


# ---------------------------------------------------------------------------
# In-memory Redis
# ---------------------------------------------------------------------------


def _expire(r, key):
    value, _ = r._store[key]
    r._store[key] = (value, datetime.now(timezone.utc) - timedelta(seconds=1))


def test_memory_redis_strings_and_ttl():
    r = MemoryRedisClient()
    assert (r.get("k"), r.ttl("k")) == (None, -2)
    r.set("k", 1)
    assert (r.get("k"), r.ttl("k")) == ("1", -1)
    r.set("t", "v", ex=60)
    assert 58 <= r.ttl("t") <= 60
    _expire(r, "t")
    assert r.ttl("t") == -2
    r.set("g", "v", ex=60)
    _expire(r, "g")
    assert r.get("g") is None


def test_memory_redis_sorted_sets():
    r = MemoryRedisClient()
    assert r.zadd("z", {"a": 1, "b": 2}) == 2
    assert r.zadd("z", {"a": 5}) == 0  # update, not a new member
    assert r.zincrby("z", 2.5, "b") == 4.5
    assert r.zincrby("new", 1, "x") == 1.0
    assert (r.zscore("z", "a"), r.zscore("z", "zz"), r.zscore("none", "a")) == (
        5.0,
        None,
        None,
    )
    assert r.zrevrange("z", 0, -1) == ["a", "b"]
    assert r.zrevrange("z", 0, 0, withscores=True) == [("a", 5.0)]
    assert r.zrevrange("missing", 0, -1) == []


def test_memory_redis_keys_and_delete():
    r = MemoryRedisClient()
    r.set("leaderboard:old", "v", ex=60)
    _expire(r, "leaderboard:old")
    r.set("leaderboard:str", "v")
    r.zadd("leaderboard:course:1", {"a": 1})
    r.zadd("other", {"a": 1})
    assert sorted(r.keys("leaderboard:*")) == [
        "leaderboard:course:1",
        "leaderboard:str",
    ]
    r.delete("leaderboard:course:1")
    assert r.zrevrange("leaderboard:course:1", 0, -1) == []
