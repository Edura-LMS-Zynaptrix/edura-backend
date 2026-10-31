"""User service on SQLite: profiles, admin listing, roles, streams and subjects,
referrals and the wallet, the signup/enrollment consumer and startup seeding.
test_users.py covers similar ground against Postgres when DATABASE_URL is set;
these run everywhere."""

import event_consumer
import jwt
import main
import profile_service
import pytest
import referrals
import router
from database import Base, get_db
from event_consumer import handle, resync
from fastapi.testclient import TestClient
from main import app
from models import (
    GradeSubject,
    ProfileRole,
    ReferralEvent,
    ReplicaAccount,
    Stream,
    UserProfile,
    WalletTransaction,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import auth
from shared import events as ev

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


def _auth(user_id: int, role: str, **claims) -> dict:
    # shared.auth reads SECRET_KEY once (test_auth.py sets its own first).
    token = jwt.encode(
        {"sub": str(user_id), "role": role, **claims},
        auth.SECRET_KEY,
        algorithm=auth.ALGORITHM,
    )
    return {"Authorization": f"Bearer {token}"}


ADMIN = _auth(1, "admin")
STUDENT = _auth(101, "student", email="ama@example.com")
OTHER_STUDENT = _auth(102, "student")
TEACHER = _auth(5, "teacher")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setitem(app.dependency_overrides, get_db, override_get_db)
    monkeypatch.setattr(event_consumer, "SessionLocal", TestingSessionLocal)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    published = []

    def publish(key, payload):
        published.append((key, payload))
        return True

    monkeypatch.setattr(profile_service, "publish", publish)
    monkeypatch.setattr(router, "publish", publish)
    return published


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _profile(db, user_id=101, role=ProfileRole.student, **fields) -> UserProfile:
    fields.setdefault("first_name", "Ama")
    fields.setdefault("last_name", "Perera")
    fields.setdefault("referral_code", f"CODE{user_id}")
    p = UserProfile(user_id=user_id, role=role, **fields)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _account(db, user_id, email, is_active=True):
    db.add(ReplicaAccount(user_id=user_id, email=email, is_active=is_active))
    db.commit()


# ---------------------------------------------------------------------------
# App wiring and seeding
# ---------------------------------------------------------------------------


def test_given_running_service_when_health_requested_then_reports_ok():
    # Act and assert
    assert client.get("/health").json() == {"status": "ok", "service": "user_service"}


@pytest.fixture
def startup(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("consumer"))
    monkeypatch.setattr(
        main.messaging, "resync_later", lambda task: calls.append(("resync", task))
    )
    return calls


def test_given_empty_database_when_service_starts_twice_then_reference_data_is_seeded_once(
    db, startup
):
    # Arrange
    main.on_startup()

    main.on_startup()

    # Act and assert
    assert db.query(GradeSubject).count() == 6 * len(main._OL_SUBJECTS)
    assert db.query(Stream).count() == len(main._AL_STREAMS)
    assert startup[:2] == ["consumer", ("resync", event_consumer.resync)]


def test_given_existing_catalogue_when_service_starts_then_seeding_keeps_it(
    db, startup
):
    # Arrange
    db.add(Stream(name="Only stream", subjects=[]))
    db.add(GradeSubject(name="Only subject", grade=6))
    db.commit()

    main._seed_reference_data()

    # Act and assert
    assert db.query(Stream).count() == 1 and db.query(GradeSubject).count() == 1


def test_given_seeding_fails_when_service_starts_then_failure_is_logged_not_raised(
    monkeypatch,
):
    no_tables = create_engine("sqlite://")
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=no_tables))
    main._seed_reference_data()  # "no such table" is caught
    no_tables.dispose()


def test_given_table_creation_fails_when_startup_runs_then_service_still_starts(
    startup, monkeypatch
):
    # Arrange
    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)

    # Act
    main.on_startup()

    # Assert
    assert "consumer" in startup


# ---------------------------------------------------------------------------
# Event consumer: signups, account changes, enrollments
# ---------------------------------------------------------------------------


def _registered(user_id=101, role="student", profile=None, **extra):
    data = {"user_id": user_id, "email": f"u{user_id}@example.com", "role": role}
    if profile is not None:
        data["profile"] = profile
    data.update(extra)
    return data


def test_given_signup_event_when_consumed_then_profile_and_account_copy_are_created(
    db, sent
):
    # Arrange
    handle(
        ev.AUTH_USER_REGISTERED,
        _registered(profile={"first_name": "Ama", "last_name": "Perera", "grade": 10}),
    )

    # Act
    profile = db.query(UserProfile).one()

    # Assert
    assert (profile.user_id, profile.role, profile.grade) == (
        101,
        ProfileRole.student,
        10,
    )
    assert profile.referral_code.startswith("AMA")

    # Act and assert
    assert db.get(ReplicaAccount, 101).email == "u101@example.com"

    # Act and assert
    assert sent[-1][0] == ev.USER_PROFILE_UPSERTED
    assert sent[-1][1]["full_name"] == "Ama Perera"


def test_given_signup_event_when_consumed_twice_then_only_one_profile_exists(db):
    # Arrange
    body = _registered(profile={"first_name": "Ama", "last_name": "Perera"})
    handle(ev.AUTH_USER_REGISTERED, body)
    handle(ev.AUTH_USER_REGISTERED, body)

    # Act and assert
    assert db.query(UserProfile).count() == 1


def test_given_signup_event_without_form_when_consumed_then_only_the_account_is_stored(
    db,
):
    # Arrange
    handle(ev.AUTH_USER_REGISTERED, _registered(user_id=1, role="admin"))

    # Act and assert
    assert db.query(UserProfile).count() == 0

    # Act and assert
    assert db.get(ReplicaAccount, 1) is not None


def test_given_signup_event_without_role_when_consumed_then_role_defaults_to_student(
    db,
):
    # Arrange
    handle(
        ev.AUTH_USER_REGISTERED,
        _registered(role=None, profile={"first_name": "A", "last_name": "B"}),
    )

    # Act and assert
    assert db.query(UserProfile).one().role == ProfileRole.student


def test_given_account_update_event_when_consumed_then_no_profile_is_created(db):
    # Arrange
    handle(
        ev.AUTH_ACCOUNT_UPDATED,
        _registered(profile={"first_name": "A", "last_name": "B"}, is_active=False),
    )

    # Act and assert
    assert db.query(UserProfile).count() == 0

    # Act and assert
    assert db.get(ReplicaAccount, 101).is_active is False


def test_given_referred_student_when_enrollment_event_consumed_twice_then_referrer_is_credited_once(
    db,
):
    # Act
    referrer = _profile(db, 50, referral_code="REF50")
    handle(
        ev.AUTH_USER_REGISTERED,
        _registered(
            profile={"first_name": "New", "last_name": "Kid", "referral_code": "ref50"}
        ),
    )
    handle(ev.ENROLLMENT_ACTIVATED, {"student_id": 101, "source": "payhere"})
    handle(ev.ENROLLMENT_ACTIVATED, {"student_id": 101, "source": "payhere"})

    db.refresh(referrer)

    # Assert
    assert referrer.wallet_credits == referrals.CREDIT_AMOUNT
    assert referrer.total_successful_referrals == 1

    (tx,) = db.query(WalletTransaction).all()

    # Act and assert
    assert (tx.user_id, tx.transaction_type, tx.balance_after) == (
        50,
        "referral_reward",
        referrals.CREDIT_AMOUNT,
    )
    assert db.query(ReferralEvent).one().status == "credited"


def test_given_manual_grant_or_missing_student_when_enrollment_event_consumed_then_no_credit_is_earned(
    db,
):
    # Act
    referrer = _profile(db, 50, referral_code="REF50")
    referred = _profile(db, 101)
    referrals.bind_referral(db, referred, referrer)
    db.commit()

    handle(ev.ENROLLMENT_ACTIVATED, {"student_id": 101, "source": "manual"})
    handle(ev.ENROLLMENT_ACTIVATED, {"source": "payhere"})
    handle("something.else", {})
    db.refresh(referrer)

    # Assert
    assert referrer.wallet_credits == 0


def test_given_handler_error_when_event_processed_then_session_rolled_back_and_error_reraised(
    monkeypatch,
):
    # Arrange
    class FakeSession:
        rolled_back = closed = False

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    fake = FakeSession()

    def explode(*args, **kwargs):
        raise ValueError("bad event")

    monkeypatch.setattr(event_consumer, "SessionLocal", lambda: fake)
    monkeypatch.setattr(event_consumer.replicas, "apply", explode)

    # Act and assert
    with pytest.raises(ValueError):
        handle(ev.AUTH_USER_REGISTERED, {})

    # Act and assert
    assert fake.rolled_back and fake.closed


def test_given_consumer_when_started_then_binds_user_queue(monkeypatch):
    # Arrange
    calls = []
    monkeypatch.setattr(
        event_consumer.messaging, "start_consumer", lambda *args: calls.append(args)
    )

    # Act
    event_consumer.start()

    # Assert
    assert calls == [("user", "user.queue", ev.QUEUES["user.queue"], handle)]


def test_given_streams_and_profiles_when_resync_runs_then_streams_then_profiles_are_announced(
    db, sent
):
    # Arrange
    db.add(Stream(name="Commerce", subjects=["Accounting"]))
    db.commit()
    _profile(db, 101)

    # Act
    resync()

    # Assert
    assert [k for k, _ in sent] == [ev.USER_STREAM_UPSERTED, ev.USER_PROFILE_UPSERTED]
    assert sent[0][1]["subjects"] == ["Accounting"]


# ---------------------------------------------------------------------------
# Admin listing and stats
# ---------------------------------------------------------------------------


def test_given_users_when_admin_lists_them_then_newest_first_with_account_details(db):
    # Arrange
    _profile(db, 101, first_name="Ama")
    _profile(db, 102, first_name="Kamal")
    _account(db, 101, "ama@example.com", is_active=False)

    # Act
    data = client.get("/", headers=ADMIN).json()

    # Assert
    assert data["total"] == 2
    ama = {u["id"]: u for u in data["items"]}[101]
    kamal = {u["id"]: u for u in data["items"]}[102]

    # Act and assert
    assert [u["id"] for u in data["items"]] == [102, 101]
    assert (ama["email"], ama["is_active"]) == ("ama@example.com", False)
    assert (kamal["email"], kamal["is_active"]) == (None, None)
    assert ama["name"] == "Ama Perera"


def test_given_filters_and_paging_when_admin_lists_users_then_results_are_filtered_and_invalid_input_rejected(
    db,
):
    # Arrange
    _profile(db, 101, first_name="Ama")
    _profile(db, 102, first_name="Kamal", last_name="Silva")
    _profile(db, 5, role=ProfileRole.teacher, first_name="Mr", last_name="Fernando")

    def ids(query):
        return [u["id"] for u in client.get(f"/{query}", headers=ADMIN).json()["items"]]

    # Act and assert
    assert ids("?role=teacher") == [5]
    assert ids("?search=silv") == [102]
    assert ids("?search=perera") == [101]
    assert ids("?page=2&page_size=2") == [101]

    # Act
    bad = client.get("/?role=parent", headers=ADMIN)

    # Assert
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "INVALID_ROLE"

    # Act and assert
    assert client.get("/?page_size=101", headers=ADMIN).status_code == 422


def test_given_non_admin_or_no_token_when_admin_routes_called_then_403_or_401(db):
    for path in ("/", "/stats"):
        assert client.get(path, headers=STUDENT).status_code == 403
        assert client.get(path).status_code == 401


def test_given_users_of_each_role_when_role_stats_requested_then_counts_per_role_returned(
    db,
):
    # Arrange
    _profile(db, 101)
    _profile(db, 102)
    _profile(db, 5, role=ProfileRole.teacher)
    _profile(db, 1, role=ProfileRole.admin)

    # Act and assert
    assert client.get("/stats", headers=ADMIN).json() == {
        "total_students": 2,
        "total_teachers": 1,
        "total_admins": 1,
    }


# ---------------------------------------------------------------------------
# Own profile
# ---------------------------------------------------------------------------


def test_given_signed_in_user_when_creating_own_profile_then_role_and_email_come_from_the_token(
    db, sent
):
    # Act
    resp = client.post(
        "/",
        json={"first_name": "Ama", "last_name": "Perera", "mobile_no": "077-123 4567"},
        headers=STUDENT,
    )

    # Assert
    assert resp.status_code == 201

    # Act
    data = resp.json()

    # Assert
    assert (data["id"], data["role"], data["email"]) == (
        101,
        "student",
        "ama@example.com",
    )
    assert data["mobile_no"] == "0771234567"
    assert sent[-1][0] == ev.USER_PROFILE_UPSERTED


def test_given_existing_profile_when_creating_again_then_409_profile_exists(db):
    # Arrange
    _profile(db, 101)

    # Act
    resp = client.post("/", json={"first_name": "A", "last_name": "B"}, headers=STUDENT)

    # Assert
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "PROFILE_EXISTS"


@pytest.mark.parametrize(
    "extra",
    [
        {"mobile_no": "12345"},
        {"district": " "},
        {"grade": 4},
        {"grade": 12},  # A/L students must give their NIC
        {"grade": 12, "nic_number": "1234"},
    ],
)
def test_given_invalid_profile_when_created_then_422(extra):
    # Arrange
    body = {"first_name": "A", "last_name": "B", **extra}

    # Act and assert
    assert client.post("/", json=body, headers=STUDENT).status_code == 422


def test_given_valid_referral_code_when_creating_profile_then_referral_is_recorded(db):
    # Arrange
    _profile(db, 50, referral_code="REF50")

    # Act
    resp = client.post(
        "/",
        json={
            "first_name": "A",
            "last_name": "B",
            "grade": 12,
            "nic_number": "200012345678",
            "referral_code": " ref50 ",
        },
        headers=STUDENT,
    )

    # Assert
    assert resp.json()["referred_by_user_id"] == 50
    assert db.query(ReferralEvent).one().status == "pending"


def test_given_unknown_referral_code_when_creating_profile_then_code_is_ignored(db):
    # Act
    resp = client.post(
        "/",
        json={"first_name": "A", "last_name": "B", "referral_code": "NOPE"},
        headers=STUDENT,
    )

    # Assert
    assert resp.status_code == 201 and resp.json()["referred_by_user_id"] is None


def test_given_students_when_reading_profiles_then_own_is_allowed_other_is_403_and_unknown_is_404(
    db,
):
    # Arrange
    _profile(db, 101)
    _profile(db, 102)
    _account(db, 102, "kamal@example.com")

    # Act
    own = client.get("/101", headers=STUDENT).json()

    # Assert
    assert own["email"] == "ama@example.com"  # from the caller's own token

    # Act and assert
    assert client.get("/102", headers=ADMIN).json()["email"] == "kamal@example.com"

    # Act
    resp = client.get("/102", headers=STUDENT)

    # Assert
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"

    # Act and assert
    assert (
        client.get("/999", headers=ADMIN).json()["detail"]["error"] == "USER_NOT_FOUND"
    )


def test_given_profile_without_email_when_read_then_email_comes_from_the_account_copy(
    db,
):
    # Arrange
    _profile(db, 102)
    _account(db, 102, "kamal@example.com", is_active=True)

    # Act
    data = client.get("/102", headers=OTHER_STUDENT).json()

    # Assert
    assert (data["email"], data["is_active"]) == ("kamal@example.com", True)


def test_given_owner_when_updating_own_profile_then_changes_are_saved(db, sent):
    # Arrange
    _profile(db, 101)

    # Act
    resp = client.put(
        "/101",
        json={"school": "Royal College", "district": " Colombo ", "grade": 11},
        headers=STUDENT,
    )

    # Assert
    assert resp.status_code == 200

    # Act
    data = resp.json()

    # Assert
    assert (data["school"], data["district"], data["grade"]) == (
        "Royal College",
        "Colombo",
        11,
    )
    assert sent[-1][1]["school"] == "Royal College"


def test_given_other_user_or_unknown_profile_when_updating_then_403_or_404(db):
    # Arrange
    _profile(db, 102)

    # Act and assert
    assert client.put("/102", json={}, headers=STUDENT).status_code == 403
    assert client.put("/102", json={"bio": "hi"}, headers=ADMIN).status_code == 200
    assert client.put("/999", json={}, headers=ADMIN).status_code == 404


def test_given_referral_code_when_profile_is_edited_then_referral_is_recorded(db):
    # Arrange
    _profile(db, 50, referral_code="REF50")
    me = _profile(db, 101, referral_code="MINE")

    # Act
    resp = client.put("/101", json={"referral_code": "REF50"}, headers=STUDENT)

    # Assert
    assert resp.json()["referred_by_user_id"] == 50
    assert resp.json()["referral_code"] == "MINE"  # their own code is untouched
    db.refresh(me)

    # Act and assert
    assert me.referred_by_user_id == 50

    # Entering their own code, or a second code, changes nothing.
    # Act
    client.put("/101", json={"referral_code": "MINE"}, headers=STUDENT)
    db.refresh(me)

    # Assert
    assert me.referred_by_user_id == 50


def test_given_admin_when_changing_role_then_role_changes_and_invalid_unknown_or_non_admin_requests_fail(
    db, sent
):
    # Arrange
    _profile(db, 101)

    # Act
    resp = client.put("/101/role", json={"role": "teacher"}, headers=ADMIN)

    # Assert
    assert resp.json()["role"] == "teacher"
    assert sent[-1][1]["role"] == "teacher"

    # Act and assert
    assert (
        client.put("/101/role", json={"role": "god"}, headers=ADMIN).status_code == 422
    )
    assert (
        client.put("/999/role", json={"role": "admin"}, headers=ADMIN).status_code
        == 404
    )
    assert (
        client.put("/101/role", json={"role": "admin"}, headers=STUDENT).status_code
        == 403
    )


def test_given_admin_when_deleting_profile_then_204_and_unknown_profile_is_404(
    db, sent
):
    # Arrange
    _profile(db, 101)

    # Act and assert
    assert client.delete("/101", headers=ADMIN).status_code == 204

    # Act and assert
    assert db.query(UserProfile).count() == 0
    assert sent[-1] == (ev.USER_PROFILE_DELETED, {"user_id": 101})

    # Act and assert
    assert client.delete("/101", headers=ADMIN).status_code == 404


# ---------------------------------------------------------------------------
# Streams and subjects
# ---------------------------------------------------------------------------


def test_given_admin_when_managing_streams_then_streams_are_created_updated_deleted_and_announced(
    db, sent
):
    # Act
    resp = client.post(
        "/streams",
        json={"name": "Commerce", "subjects": ["Accounting", "Economics"]},
        headers=ADMIN,
    )

    # Assert
    assert resp.status_code == 201

    # Act
    stream_id = resp.json()["id"]

    # Assert
    assert sent[-1][0] == ev.USER_STREAM_UPSERTED

    # Act
    resp = client.put(f"/streams/{stream_id}", json={"is_active": False}, headers=ADMIN)

    # Assert
    assert resp.json()["is_active"] is False
    assert sent[-1][1]["is_active"] is False

    # Act and assert
    assert client.get("/streams").json() == []
    assert [s["name"] for s in client.get("/streams?active_only=false").json()] == [
        "Commerce"
    ]

    assert client.delete(f"/streams/{stream_id}", headers=ADMIN).status_code == 204

    # Act and assert
    assert sent[-1] == (ev.USER_STREAM_DELETED, {"id": stream_id})


def test_given_unknown_stream_invalid_body_or_non_admin_when_managing_streams_then_errors_returned(
    db,
):
    # Act and assert
    assert client.put("/streams/9", json={}, headers=ADMIN).status_code == 404

    # Act
    resp = client.delete("/streams/9", headers=ADMIN)

    # Assert
    assert resp.json()["detail"]["error"] == "STREAM_NOT_FOUND"

    # Act and assert
    assert client.post("/streams", json={"name": ""}, headers=ADMIN).status_code == 422
    assert (
        client.post("/streams", json={"name": "x"}, headers=TEACHER).status_code == 403
    )


def test_given_admin_when_managing_grade_subjects_then_they_are_created_updated_and_deleted(
    db,
):
    # Act
    resp = client.post(
        "/grade-subjects", json={"name": "Science", "grade": 9}, headers=ADMIN
    )

    # Assert
    assert resp.status_code == 201
    subject_id = resp.json()["id"]
    client.post("/grade-subjects", json={"name": "Art", "grade": 9}, headers=ADMIN)
    client.post("/grade-subjects", json={"name": "Maths", "grade": 10}, headers=ADMIN)

    # Act and assert
    assert [s["name"] for s in client.get("/grade-subjects?grade=9").json()] == [
        "Art",
        "Science",
    ]
    client.put(
        f"/grade-subjects/{subject_id}", json={"is_active": False}, headers=ADMIN
    )

    # Act and assert
    assert len(client.get("/grade-subjects").json()) == 2
    assert len(client.get("/grade-subjects?active_only=false").json()) == 3

    assert (
        client.delete(f"/grade-subjects/{subject_id}", headers=ADMIN).status_code == 204
    )


def test_given_unknown_subject_or_invalid_body_when_managing_grade_subjects_then_errors_returned(
    db,
):
    # Act and assert
    assert (
        client.post(
            "/grade-subjects", json={"name": "Physics", "grade": 12}, headers=ADMIN
        ).status_code
        == 422
    )
    assert client.put("/grade-subjects/9", json={}, headers=ADMIN).status_code == 404

    # Act
    resp = client.delete("/grade-subjects/9", headers=ADMIN)

    # Assert
    assert resp.json()["detail"]["error"] == "SUBJECT_NOT_FOUND"


def test_given_grade_and_stream_when_subjects_requested_then_matching_subjects_returned_and_invalid_grade_rejected(
    db,
):
    # Arrange
    db.add_all(
        [
            GradeSubject(name="Science", grade=9),
            GradeSubject(name="English", grade=9),
            GradeSubject(name="Old", grade=9, is_active=False),
            Stream(name="Bio", subjects=["Biology", "Chemistry"]),
            Stream(name="Closed", subjects=["X"], is_active=False),
        ]
    )
    db.commit()
    bio = db.query(Stream).filter_by(name="Bio").one()
    closed = db.query(Stream).filter_by(name="Closed").one()

    # Act and assert
    assert client.get("/subjects?grade=9").json() == ["English", "Science"]
    assert client.get(f"/subjects?grade=12&stream_id={bio.id}").json() == [
        "Biology",
        "Chemistry",
    ]
    assert client.get("/subjects?grade=13").json() == []
    assert client.get(f"/subjects?grade=12&stream_id={closed.id}").json() == []
    assert client.get("/subjects?grade=14").status_code == 422


# ---------------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------------


def test_given_student_when_referral_summary_requested_then_code_and_link_returned(
    db, monkeypatch
):
    # Arrange
    monkeypatch.setattr(referrals, "FRONTEND_URL", "https://edura.lk/")
    _profile(db, 101, referral_code="AMA1", wallet_credits=3)

    # Act
    data = client.get("/referral/summary", headers=STUDENT).json()

    # Assert
    assert data["referral_link"] == "https://edura.lk/register?ref=AMA1"
    assert data["wallet_credits"] == 3

    # Act
    resp = client.get("/referral/summary", headers=OTHER_STUDENT)

    # Assert
    assert resp.json()["detail"]["error"] == "PROFILE_NOT_FOUND"


def test_given_referral_events_when_listed_then_events_are_returned_and_invalid_limit_rejected(
    db,
):
    # Arrange
    me = _profile(db, 101)
    for uid, school in ((201, "Royal"), (202, None)):
        friend = _profile(db, uid, first_name=f"F{uid}", last_name="X", school=school)
        referrals.bind_referral(db, friend, me)
    db.commit()
    referrals.credit_on_enrollment(db, 201)

    # Act
    events = client.get("/referral/events", headers=STUDENT).json()["events"]
    by_id = {e["referred_student_id"]: e for e in events}

    # Assert
    assert by_id["201"]["status"] == "credited" and by_id["201"]["credited_at"]
    assert by_id["202"]["status"] == "pending" and by_id["202"]["credited_at"] is None
    assert by_id["201"]["referred_student_name"] == "F201 X"

    # Act and assert
    assert (
        len(client.get("/referral/events?limit=1", headers=STUDENT).json()["events"])
        == 1
    )
    assert client.get("/referral/events?limit=0", headers=STUDENT).status_code == 422


def test_given_valid_code_when_student_applies_it_then_referral_is_bound(db):
    # Arrange
    _profile(db, 50, referral_code="REF50")
    _profile(db, 101)

    # Act
    resp = client.post(
        "/referral/apply", json={"referral_code": "ref50"}, headers=STUDENT
    )

    # Assert
    assert resp.status_code == 200
    assert resp.json()["referrer_user_id"] == 50


@pytest.mark.parametrize(
    "setup, code, status, error",
    [
        ("plain", "  ", 400, "CODE_REQUIRED"),
        ("plain", "NOPE", 404, "INVALID_CODE"),
        ("plain", "CODE101", 400, "SELF_REFERRAL"),
        ("referred", "REF50", 400, "ALREADY_REFERRED"),
        ("event_exists", "REF50", 400, "ALREADY_PROCESSED"),
        ("teacher_profile", "REF50", 403, "STUDENTS_ONLY"),
    ],
)
def test_given_blank_invalid_own_or_repeated_code_when_applying_then_each_error_is_returned(
    db, setup, code, status, error
):
    # Arrange
    _profile(db, 50, referral_code="REF50")
    role = ProfileRole.teacher if setup == "teacher_profile" else ProfileRole.student
    _profile(
        db, 101, role=role, referred_by_user_id=50 if setup == "referred" else None
    )
    if setup == "event_exists":
        db.add(ReferralEvent(referrer_user_id=7, referred_user_id=101))
        db.commit()

    # Act
    resp = client.post("/referral/apply", json={"referral_code": code}, headers=STUDENT)

    # Assert
    assert resp.status_code == status
    assert resp.json()["detail"]["error"] == error


def test_given_user_without_student_profile_when_applying_code_then_403_profile_not_found(
    db,
):
    # Act and assert
    assert (
        client.post(
            "/referral/apply", json={"referral_code": "X"}, headers=STUDENT
        ).json()["detail"]["error"]
        == "PROFILE_NOT_FOUND"
    )
    assert (
        client.post(
            "/referral/apply", json={"referral_code": "X"}, headers=TEACHER
        ).status_code
        == 403
    )


def test_given_missing_pending_event_or_referrer_when_credit_applied_then_nothing_is_credited(
    db,
):
    # Act and assert
    assert referrals.credit_on_enrollment(db, 101) is False
    db.add(ReferralEvent(referrer_user_id=404, referred_user_id=101, status="pending"))
    db.commit()

    # Act and assert
    assert referrals.credit_on_enrollment(db, 101) is False  # referrer was deleted


def test_given_own_code_or_existing_referral_when_binding_referral_then_it_is_refused(
    db,
):
    # Arrange
    a = _profile(db, 101)

    b = _profile(db, 102, referred_by_user_id=50)

    # Act and assert
    assert referrals.bind_referral(db, a, a) is None
    assert referrals.bind_referral(db, b, a) is None


def test_given_blank_code_when_finding_referrer_then_no_referrer_is_found(db):
    # Act and assert
    assert referrals.find_referrer(db, None) is None
    assert referrals.find_referrer(db, "   ") is None


# ---------------------------------------------------------------------------
# profile_service helpers
# ---------------------------------------------------------------------------


def test_given_names_when_referral_code_generated_then_prefix_comes_from_the_name_with_a_user_fallback_and_length_is_six(
    db,
):
    # Act and assert
    assert profile_service.generate_referral_code(db, "o'Neil").startswith("ONEI")
    assert profile_service.generate_referral_code(db, "123").startswith("USER")
    assert len(profile_service.generate_referral_code(db, "Al")) == 6


def test_given_colliding_codes_when_referral_code_generated_then_it_falls_back_to_another_code(
    db, monkeypatch
):
    # With every random character forced to "A", each try produces "AMA" + "AAAA".
    # Arrange
    _profile(db, 101, referral_code="AMAAAAA")
    monkeypatch.setattr(profile_service.secrets, "choice", lambda alphabet: "A")

    # Act
    code = profile_service.generate_referral_code(db, "Ama")

    # Assert
    assert code != "AMAAAAA" and len(code) == 12  # token_hex(6)


def test_given_profile_when_event_payload_built_then_it_carries_the_profile_fields(db):
    # Arrange
    p = _profile(db, 101, first_name="Ama", last_name="")

    # Act
    payload = profile_service.profile_event(p)

    # Assert
    assert payload["full_name"] == "Ama" and payload["role"] == "student"
    p.created_at = None

    # Act and assert
    assert profile_service.profile_event(p)["created_at"] is None


def test_given_stream_without_optional_fields_when_event_built_then_defaults_are_used():
    # Act
    payload = profile_service.stream_event(Stream(id=1, name="X", subjects=None))

    # Assert
    assert payload["subjects"] == []
