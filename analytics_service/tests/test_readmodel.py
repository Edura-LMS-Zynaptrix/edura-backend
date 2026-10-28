"""Analytics builds every report from events alone. These tests feed it events and read the reports back."""

import asyncio
from datetime import datetime, timedelta, timezone

import dashboard
import event_consumer
import pytest
import readmodel
from database import Base
from progress import course_struggle, progress_summary, student_progress
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def bus(monkeypatch):
    """An empty read model on SQLite, and a function that delivers an event to it."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(event_consumer, "SessionLocal", session)
    monkeypatch.setattr(readmodel, "SessionLocal", session)
    yield event_consumer.handle
    Base.metadata.drop_all(bind=engine)


def run(coro):
    return asyncio.run(coro)


NOW = datetime.now(timezone.utc)


def _seed_school(send):
    """Two students, one course with two lessons and two exams, one student enrolled."""
    for uid, name, grade in ((7, "Nimal Perera", 11), (8, "Kamal Silva", 11)):
        send(
            "auth.user-registered",
            {
                "user_id": uid,
                "email": f"u{uid}@example.com",
                "role": "student",
                "is_active": True,
                "is_email_verified": True,
                "created_at": "2026-09-01T00:00:00+00:00",
            },
        )
        send(
            "user.profile-upserted",
            {
                "user_id": uid,
                "role": "student",
                "full_name": name,
                "first_name": name.split()[0],
                "last_name": name.split()[1],
                "school": "Royal",
                "district": "Colombo",
                "grade": grade,
                "mobile_no": "0771234567",
                "created_at": "2026-09-02T00:00:00+00:00",
            },
        )
    send(
        "course.upserted",
        {
            "id": 10,
            "title": "Mathematics",
            "grade": 11,
            "status": "PUBLISHED",
            "course_type": "exam",
            "price": 1500,
            "instructor_id": 2,
        },
    )
    for mid, title, pos in ((100, "Algebra", 1), (200, "Geometry", 2)):
        send(
            "course.module-upserted",
            {"id": mid, "course_id": 10, "title": title, "position": pos},
        )
    for eid, title, module in ((1, "Algebra quiz", 100), (2, "Geometry quiz", 200)):
        send(
            "assessment.upserted",
            {
                "id": eid,
                "course_id": 10,
                "module_id": module,
                "title": title,
                "position": eid,
                "is_published": True,
                "created_at": NOW.isoformat(),
            },
        )
    send(
        "access.changed",
        {"student_id": 7, "courses": [{"id": 10, "until": None}], "lessons": []},
    )
    send("enrollment.changed", {"student_id": 7, "course_id": 10, "status": "ACTIVE"})


def _graded(send, submission, student, exam, score, minutes_ago=10, seconds=120):
    send(
        "assessment.graded",
        {
            "submission_id": submission,
            "student_id": student,
            "assessment_id": exam,
            "course_id": 10,
            "module_id": 100 if exam == 1 else 200,
            "score": score,
            "passed": score >= 50,
            "marks_obtained": int(score) // 10,
            "total_questions": 10,
            "time_seconds": seconds,
            "submitted_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        },
    )


def test_given_events_when_student_progress_read_then_it_comes_from_events_only(bus):
    # Arrange
    _seed_school(bus)
    _graded(bus, 1, 7, 1, 40, minutes_ago=30)
    _graded(bus, 2, 7, 1, 90, minutes_ago=20)
    bus("badge.awarded", {"student_id": 7, "badge_key": "first_step"})

    # Act
    data = run(student_progress(7))

    # Assert
    assert data["student"]["full_name"] == "Nimal Perera"
    assert data["student"]["badges_count"] == 1
    assert data["overall"]["total_unique_exams"] == 2
    assert data["overall"]["unique_exams_attempted"] == 1
    assert data["overall"]["total_attempts_made"] == 2
    (maths,) = data["subjects"]

    # Act and assert
    assert maths["subject"] == "Mathematics" and maths["best_score_pct"] == 90
    assert [h["status"] for h in maths["exams"][0]["history"]] == ["passed", "failed"]


def test_given_no_access_when_open_courses_read_then_nothing_is_open(bus):
    # Arrange
    _seed_school(bus)
    _graded(bus, 5, 8, 1, 70)  # student 8 never got access

    # Act
    data = run(student_progress(8))

    # Assert
    assert data["overall"]["total_unique_exams"] == 0 and data["subjects"] == []


def test_given_expired_class_package_when_access_counted_then_it_is_not_counted(bus):
    # Arrange
    _seed_school(bus)
    yesterday = (NOW - timedelta(days=1)).isoformat()
    bus(
        "access.changed",
        {"student_id": 8, "courses": [{"id": 10, "until": yesterday}], "lessons": []},
    )

    # Act and assert
    assert run(student_progress(8))["overall"]["total_unique_exams"] == 0
    tomorrow = (NOW + timedelta(days=1)).isoformat()
    bus(
        "access.changed",
        {"student_id": 8, "courses": [{"id": 10, "until": tomorrow}], "lessons": []},
    )

    # Act and assert
    assert run(student_progress(8))["overall"]["total_unique_exams"] == 2


def test_given_single_lesson_grant_when_exams_listed_then_only_that_lessons_exams_are_open(
    bus,
):
    # Arrange
    _seed_school(bus)
    bus("access.changed", {"student_id": 8, "courses": [], "lessons": [200]})

    # Act
    data = run(student_progress(8))

    # Assert
    assert [e["title"] for e in data["subjects"][0]["exams"]] == ["Geometry quiz"]


def test_given_event_when_consumed_again_then_nothing_changes(bus):
    # Arrange
    _seed_school(bus)
    _graded(bus, 1, 7, 1, 80)
    _graded(bus, 1, 7, 1, 80)
    bus("badge.awarded", {"student_id": 7, "badge_key": "first_step"})
    bus("badge.awarded", {"student_id": 7, "badge_key": "first_step"})

    # Act
    data = run(student_progress(7))

    # Assert
    assert data["overall"]["total_attempts_made"] == 1
    assert data["student"]["badges_count"] == 1


def test_given_exam_with_attempts_when_deleted_then_its_attempts_are_removed(bus):
    # Arrange
    _seed_school(bus)
    _graded(bus, 1, 7, 1, 80)
    bus("assessment.deleted", {"id": 1})

    # Act
    data = run(student_progress(7))

    # Assert
    assert (
        data["overall"]["total_unique_exams"] == 1
        and data["overall"]["total_attempts_made"] == 0
    )


def test_given_students_when_summary_built_then_each_is_listed_with_completion(bus):
    # Arrange
    _seed_school(bus)
    _graded(bus, 1, 7, 1, 100)

    # Act
    summary = {row["full_name"]: row for row in run(progress_summary())}

    # Assert
    assert summary["Nimal Perera"]["completion_pct"] == 50
    assert summary["Nimal Perera"]["avg_score_pct"] == 100
    assert summary["Kamal Silva"]["completion_pct"] == 0


def test_given_repeated_attempts_when_struggle_analysed_then_only_first_attempts_are_used(
    bus,
):
    # Arrange
    _seed_school(bus)
    _graded(bus, 1, 7, 2, 20, minutes_ago=50)  # first attempt: fail
    _graded(bus, 2, 7, 2, 95, minutes_ago=10)  # a good retake must not hide it
    _graded(bus, 3, 8, 2, 30, minutes_ago=40)

    result = run(course_struggle(10))
    (hardest,) = result["hardest_exams"]

    # Act and assert
    assert hardest["title"] == "Geometry quiz" and hardest["failure_rate"] == 100.0
    assert result["struggling_modules"][0]["title"] == "Geometry"
    assert run(course_struggle(999)) is None


def test_given_payments_and_receipts_when_dashboard_numbers_built_then_totals_match(
    bus,
):
    # Arrange
    _seed_school(bus)
    _graded(bus, 1, 7, 1, 70)
    bus(
        "payment.success",
        {
            "order_id": "MANUAL-1",
            "student_id": 7,
            "course_id": 10,
            "amount": 1500,
            "completed_at": NOW.isoformat(),
        },
    )
    bus(
        "payment.success",
        {
            "order_id": "FREE-2",
            "student_id": 8,
            "course_id": 10,
            "amount": 0,
            "completed_at": NOW.isoformat(),
        },
    )
    bus("payment.receipt-uploaded", {"receipt_id": 1, "payment_id": 1, "student_id": 7})
    bus("payment.receipt-uploaded", {"receipt_id": 2, "payment_id": 2, "student_id": 8})
    bus(
        "payment.receipt-reviewed",
        {
            "receipt_id": 1,
            "student_id": 7,
            "status": "APPROVED",
            "reviewed_at": NOW.isoformat(),
        },
    )

    # Act
    stats = run(dashboard.admin_stats())

    # Assert
    assert stats["total_students"] == 2 and stats["total_courses"] == 1
    assert stats["total_exams"] == 2 and stats["total_exam_attempts"] == 1
    assert stats["total_revenue"] == 1500.0  # the free enrollment is not revenue
    assert stats["pending_bank_slips"] == 1
    assert any(
        a["type"] == "payment" and "Nimal Perera" in a["subtitle"]
        for a in stats["recent_activity"]
    )

    # Act
    analytics = run(dashboard.admin_analytics())

    # Assert
    assert analytics["students_by_grade"] == [{"grade": 11, "count": 2}]
    assert analytics["students_by_district"] == [{"district": "Colombo", "count": 2}]
    assert analytics["top_exams"][0]["title"] == "Algebra quiz"
    assert analytics["enrollments_by_subject"] == [
        {"subject": "Mathematics", "count": 1}
    ]


def test_given_deleted_courses_and_profiles_when_read_then_they_no_longer_appear(bus):
    # Arrange
    _seed_school(bus)
    bus("course.deleted", {"id": 10})
    bus("user.profile-deleted", {"user_id": 8})

    # Act and assert
    assert run(readmodel.course_info(10)) is None
    assert run(readmodel.profile(8)) is None
    assert run(readmodel.profile(7))["full_name"] == "Nimal Perera"


def test_given_read_model_data_when_public_platform_numbers_built_then_counts_are_returned(
    bus,
):
    # Arrange
    _seed_school(bus)

    # Act
    sources = run(readmodel.fetch_platform_sources())

    # Assert
    assert sources["users"]["total_students"] == 2
    assert [c["title"] for c in sources["courses"]] == ["Mathematics"]
    assert sources["enrollments"] == [{"course_id": 10, "count": 1}]
