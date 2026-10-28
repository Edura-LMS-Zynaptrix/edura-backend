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


def test_given_running_service_when_health_requested_then_reports_ok():
    # Act and assert
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "progress_service",
    }


def test_given_service_starting_when_lifespan_runs_then_tables_created_and_consumer_thread_started(
    monkeypatch,
):
    # Arrange
    fresh = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    started = threading.Event()
    monkeypatch.setattr(main, "engine", fresh)
    monkeypatch.setattr(consumer, "start_consumer", started.set)

    # Act and assert
    with TestClient(app):
        assert started.wait(timeout=5)

    with fresh.connect() as conn:
        assert "course_progress" in fresh.dialect.get_table_names(conn)
    fresh.dispose()


def test_given_table_creation_fails_when_lifespan_runs_then_service_still_starts(
    monkeypatch,
):
    # Arrange
    started = threading.Event()

    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    monkeypatch.setattr(consumer, "start_consumer", started.set)

    # Act and assert
    with TestClient(app):
        assert started.wait(timeout=5)


# ---------------------------------------------------------------------------
# Lesson progress routes
# ---------------------------------------------------------------------------


def test_given_student_when_lesson_marked_watched_then_it_appears_in_lesson_progress_list():
    # Act
    resp = _watch(
        lesson_id=3,
        total_lessons=4,
        watch_duration_seconds=120,
        last_position_seconds=118,
    )

    # Assert
    assert resp.status_code == 200
    assert resp.json() == {
        "lesson_id": 3,
        "is_completed": True,
        "watch_duration_seconds": 120,
        "last_position_seconds": 118,
    }
    _watch(course_id=11, lesson_id=9)  # another course

    # Act
    rows = client.get(
        "/api/progress/courses/10/lessons/progress", headers=STUDENT
    ).json()

    # Assert
    assert [r["lesson_id"] for r in rows] == [3]
    # The legacy un-prefixed paths serve the same data.
    # Act and assert
    assert client.get("/courses/10/lessons/progress", headers=STUDENT).json() == rows


def test_given_lesson_already_watched_when_rewatched_then_longest_watch_time_is_kept(
    db,
):
    # Arrange
    _watch(lesson_id=1, watch_duration_seconds=300, last_position_seconds=300)

    resp = _watch(lesson_id=1, watch_duration_seconds=60, last_position_seconds=60)

    # Act and assert
    assert (
        resp.json()["watch_duration_seconds"],
        resp.json()["last_position_seconds"],
    ) == (
        300,
        60,
    )
    assert db.query(LessonProgress).count() == 1


def test_given_non_student_missing_token_or_bad_body_when_marking_watched_then_rejected():
    # Act and assert
    assert _watch(headers=_auth(5, "teacher")).status_code == 403
    assert _watch(headers={}).status_code == 401
    assert _watch(lesson_id="x").status_code == 422


def test_given_student_without_progress_when_dashboard_requested_then_empty_progress_returned():
    # Act
    data = client.get("/api/progress/courses/10", headers=STUDENT).json()

    # Assert
    assert data == {
        "completion_percentage": 0.0,
        "watched_lessons": 0,
        "total_lessons": 0,
        "assessment_scores": [],
        "certificate_issued": False,
    }


def test_given_token_without_user_when_dashboard_requested_then_401():
    # Act
    resp = client.get("/courses/10", headers=_auth(0))

    # Assert
    assert resp.status_code == 401


def test_given_recorded_activity_when_dashboard_requested_then_progress_is_reflected(
    db,
):
    # Arrange
    _watch(lesson_id=1, total_lessons=2)
    services.process_assessment_graded(db, 101, 10, 7, 88.0, True)

    # Act
    data = client.get("/courses/10", headers=_auth(101, "teacher")).json()

    # Assert
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


def test_given_no_redis_ranking_when_leaderboard_requested_then_database_is_used(db):
    # Arrange
    db.add_all(
        [
            LeaderboardEntry(student_id=1, course_id=10, points=30),
            LeaderboardEntry(student_id=2, course_id=10, points=90),
            LeaderboardEntry(student_id=3, course_id=11, points=99),
        ]
    )
    db.commit()

    # Act
    rows = client.get("/api/progress/courses/10/leaderboard", headers=STUDENT).json()

    # Assert
    assert rows == [
        {"rank": 1, "student_id": 2, "points": 90},
        {"rank": 2, "student_id": 1, "points": 30},
    ]


def test_given_redis_ranking_when_leaderboard_requested_then_redis_is_used(db):
    # Arrange
    services.get_redis_client().zadd("leaderboard:course:10", {"5": 40, "6": 70})
    db.add(LeaderboardEntry(student_id=1, course_id=10, points=999))
    db.commit()

    # Act
    rows = client.get("/courses/10/leaderboard", headers=STUDENT).json()

    # Assert
    assert [r["student_id"] for r in rows] == [6, 5]


def test_given_more_than_ten_students_when_leaderboard_requested_then_only_ten_returned(
    db,
):
    # Arrange
    services.get_redis_client().zadd(
        "leaderboard:course:10", {str(i): i for i in range(15)}
    )

    # Act
    rows = client.get("/courses/10/leaderboard", headers=STUDENT).json()

    # Assert
    assert len(rows) == 10 and rows[0]["student_id"] == 14


# ---------------------------------------------------------------------------
# Completion and certificates
# ---------------------------------------------------------------------------


def test_given_unknown_lesson_count_when_progress_calculated_then_one_lesson_is_the_whole_course(
    db, certificates
):
    # Arrange
    services.process_lesson_watched(db, 101, 10, 1)

    # Act
    cp = db.query(CourseProgress).one()

    # Assert
    assert (float(cp.completion_percentage), cp.total_lessons_count) == (100.0, 1)
    assert cp.status == CourseProgressStatus.completed
    assert len(certificates) == 1


def test_given_completed_course_when_more_lessons_added_then_completion_drops_but_first_completion_date_is_kept(
    db,
):
    # Arrange
    services.process_lesson_watched(db, 101, 10, 1, total_lessons=1)
    first_done = db.query(CourseProgress).one().completed_at

    services.process_lesson_watched(db, 101, 10, 2, total_lessons=4)
    db.expire_all()

    # Act
    cp = db.query(CourseProgress).one()

    # Assert
    assert (float(cp.completion_percentage), cp.status) == (
        50.0,
        CourseProgressStatus.in_progress,
    )
    assert cp.completed_at == first_done

    services.process_lesson_watched(db, 101, 10, 3)  # total stays 4
    db.expire_all()

    # Act and assert
    assert float(db.query(CourseProgress).one().completion_percentage) == 75.0


def test_given_failed_assessment_when_course_completed_then_certificate_is_not_issued(
    db, certificates
):
    # Arrange
    services.process_assessment_graded(db, 101, 10, 1, 30.0, False)

    services.process_lesson_watched(db, 101, 10, 1, total_lessons=1)

    # Act and assert
    assert certificates == [] and db.query(Certificate).count() == 0

    services.process_assessment_graded(db, 101, 10, 1, 90.0, True)  # retake passes

    # Act and assert
    assert len(certificates) == 1
    assert certificates[0]["certificate_id"] == db.query(Certificate).one().id


def test_given_issued_certificate_when_completion_repeats_then_certificate_is_not_issued_again(
    db, certificates
):
    # Arrange
    services.process_lesson_watched(db, 101, 10, 1, total_lessons=1)

    services.process_assessment_graded(db, 101, 10, 2, 100.0, True)

    # Act and assert
    assert db.query(Certificate).count() == 1 and len(certificates) == 1
    assert services.check_certificate_eligibility(db, 101, 10).certificate_url == (
        "/certificates/101_10.pdf"
    )


def test_given_scores_and_completion_when_points_calculated_then_points_are_scores_plus_bonus(
    db,
):
    # Arrange
    services.process_assessment_graded(db, 101, 10, 1, 75.6, True)

    # Act and assert
    assert services.calculate_total_points(db, 101, 10) == 75

    services.process_lesson_watched(db, 101, 10, 1, total_lessons=1)

    # Act and assert
    assert services.calculate_total_points(db, 101, 10) == 85

    entry = db.query(LeaderboardEntry).one()

    # Act and assert
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


def test_given_incomplete_events_when_consumed_then_acknowledged_without_changes(db):
    # Arrange
    for key, body in [
        ("lesson.watched", {"student_id": 101, "course_id": 10}),
        ("assessment.graded", {"student_id": 101, "score": 50}),
        ("something.else", {"student_id": 101}),
    ]:
        _deliver(key, body).basic_ack.assert_called_once_with(delivery_tag=4)

    # Act and assert
    assert db.query(LessonProgress).count() == 0
    assert db.query(LeaderboardEntry).count() == 0


def test_given_graded_event_without_course_when_consumed_then_it_is_recorded_for_course_one(
    db,
):
    # Arrange
    _deliver(
        "assessment.graded", {"student_id": 101, "assessment_id": 5, "score": "60"}
    )

    # Act and assert
    assert services.get_assessment_results(101, 1) == [
        {"assessment_id": 5, "score": 60.0, "passed": False}
    ]


def test_given_lesson_watched_event_when_consumed_then_progress_is_recorded(db):
    # Arrange
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

    # Act and assert
    assert db.query(LessonProgress).one().watch_duration_seconds == 30


def test_given_malformed_messages_when_consumed_then_they_are_dropped(db):
    # Act and assert
    _deliver("lesson.watched", "{not json").basic_nack.assert_called_once_with(
        delivery_tag=4, requeue=False
    )

    # Act
    ch = _deliver(
        "assessment.graded", {"student_id": 1, "assessment_id": 1, "score": "x"}
    )

    # Assert
    ch.basic_nack.assert_called_once()


def test_given_unavailable_broker_when_consumer_runs_then_it_reconnects_until_broker_answers(
    monkeypatch,
):
    # Arrange
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

    # Act
    consumer.start_consumer()

    # Assert
    assert sleeps == [5]

    keys = [c.kwargs["routing_key"] for c in channel.queue_bind.call_args_list]

    # Act and assert
    assert keys == ["lesson.watched", "assessment.graded"]
    channel.start_consuming.assert_called_once()


# ---------------------------------------------------------------------------
# certificate.issued publisher
# ---------------------------------------------------------------------------


def test_given_certificate_when_event_published_then_message_is_durable(monkeypatch):
    # Arrange
    channel = MagicMock()
    conn = MagicMock()
    conn.channel.return_value = channel
    monkeypatch.setattr(events.pika, "BlockingConnection", lambda params: conn)

    issued = datetime(2026, 10, 1, tzinfo=timezone.utc)
    events.publish_certificate_issued(101, 10, 3, issued)

    kwargs = channel.basic_publish.call_args.kwargs

    # Act and assert
    assert kwargs["routing_key"] == "certificate.issued"
    assert json.loads(kwargs["body"]) == {
        "student_id": 101,
        "course_id": 10,
        "certificate_id": 3,
        "issued_at": issued.isoformat(),
    }
    assert kwargs["properties"].delivery_mode == 2
    conn.close.assert_called_once()


def test_given_publish_failure_or_missing_time_when_certificate_event_published_then_time_defaults_and_no_error_raised(
    monkeypatch,
):
    # Arrange
    def down(params):
        raise ConnectionError("broker down")

    monkeypatch.setattr(events.pika, "BlockingConnection", down)
    events.publish_certificate_issued(101, 10, 3)  # logged, not raised

    channel = MagicMock()
    conn = MagicMock(channel=MagicMock(return_value=channel))
    monkeypatch.setattr(events.pika, "BlockingConnection", lambda params: conn)
    events.publish_certificate_issued(101, 10, 3)

    # Act
    issued_at = json.loads(channel.basic_publish.call_args.kwargs["body"])["issued_at"]

    # Assert
    assert datetime.fromisoformat(issued_at).year >= 2026


# ---------------------------------------------------------------------------
# In-memory Redis
# ---------------------------------------------------------------------------


def _expire(r, key):
    value, _ = r._store[key]
    r._store[key] = (value, datetime.now(timezone.utc) - timedelta(seconds=1))


def test_given_in_memory_redis_when_strings_with_ttl_stored_then_values_expire_as_set():
    # Arrange
    r = MemoryRedisClient()

    # Act and assert
    assert (r.get("k"), r.ttl("k")) == (None, -2)
    r.set("k", 1)

    # Act and assert
    assert (r.get("k"), r.ttl("k")) == ("1", -1)
    r.set("t", "v", ex=60)

    # Act and assert
    assert 58 <= r.ttl("t") <= 60

    _expire(r, "t")

    # Act and assert
    assert r.ttl("t") == -2
    r.set("g", "v", ex=60)
    _expire(r, "g")

    # Act and assert
    assert r.get("g") is None


def test_given_in_memory_redis_when_sorted_set_used_then_members_are_ranked():
    # Arrange
    r = MemoryRedisClient()

    # Act and assert
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


def test_given_in_memory_redis_when_keys_listed_and_deleted_then_they_are_removed():
    # Act
    r = MemoryRedisClient()
    r.set("leaderboard:old", "v", ex=60)
    _expire(r, "leaderboard:old")
    r.set("leaderboard:str", "v")
    r.zadd("leaderboard:course:1", {"a": 1})
    r.zadd("other", {"a": 1})

    # Assert
    assert sorted(r.keys("leaderboard:*")) == [
        "leaderboard:course:1",
        "leaderboard:str",
    ]

    r.delete("leaderboard:course:1")

    # Act and assert
    assert r.zrevrange("leaderboard:course:1", 0, -1) == []
