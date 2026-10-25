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


def test_health():
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


def test_startup_seeds_reference_data_once(db, startup):
    main.on_startup()
    main.on_startup()

    assert db.query(GradeSubject).count() == 6 * len(main._OL_SUBJECTS)
    assert db.query(Stream).count() == len(main._AL_STREAMS)
    assert startup[:2] == ["consumer", ("resync", event_consumer.resync)]


def test_seeding_keeps_an_existing_catalogue(db, startup):
    db.add(Stream(name="Only stream", subjects=[]))
    db.add(GradeSubject(name="Only subject", grade=6))
    db.commit()
    main._seed_reference_data()
    assert db.query(Stream).count() == 1 and db.query(GradeSubject).count() == 1


def test_seeding_failure_is_logged_not_raised(monkeypatch):
    no_tables = create_engine("sqlite://")
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=no_tables))
    main._seed_reference_data()  # "no such table" is caught
    no_tables.dispose()


def test_startup_survives_create_all_failure(startup, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    main.on_startup()
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


def test_signup_creates_profile_and_account(db, sent):
    handle(
        ev.AUTH_USER_REGISTERED,
        _registered(profile={"first_name": "Ama", "last_name": "Perera", "grade": 10}),
    )
    profile = db.query(UserProfile).one()
    assert (profile.user_id, profile.role, profile.grade) == (
        101,
        ProfileRole.student,
        10,
    )
    assert profile.referral_code.startswith("AMA")
    assert db.get(ReplicaAccount, 101).email == "u101@example.com"
    assert sent[-1][0] == ev.USER_PROFILE_UPSERTED
    assert sent[-1][1]["full_name"] == "Ama Perera"


def test_repeated_signup_event_keeps_one_profile(db):
    body = _registered(profile={"first_name": "Ama", "last_name": "Perera"})
    handle(ev.AUTH_USER_REGISTERED, body)
    handle(ev.AUTH_USER_REGISTERED, body)
    assert db.query(UserProfile).count() == 1


def test_signup_without_a_form_only_stores_the_account(db):
    handle(ev.AUTH_USER_REGISTERED, _registered(user_id=1, role="admin"))
    assert db.query(UserProfile).count() == 0
    assert db.get(ReplicaAccount, 1) is not None


def test_signup_with_missing_role_defaults_to_student(db):
    handle(
        ev.AUTH_USER_REGISTERED,
        _registered(role=None, profile={"first_name": "A", "last_name": "B"}),
    )
    assert db.query(UserProfile).one().role == ProfileRole.student


def test_account_update_never_creates_a_profile(db):
    handle(
        ev.AUTH_ACCOUNT_UPDATED,
        _registered(profile={"first_name": "A", "last_name": "B"}, is_active=False),
    )
    assert db.query(UserProfile).count() == 0
    assert db.get(ReplicaAccount, 101).is_active is False


def test_enrollment_credits_the_referrer_once(db):
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
    assert referrer.wallet_credits == referrals.CREDIT_AMOUNT
    assert referrer.total_successful_referrals == 1
    (tx,) = db.query(WalletTransaction).all()
    assert (tx.user_id, tx.transaction_type, tx.balance_after) == (
        50,
        "referral_reward",
        referrals.CREDIT_AMOUNT,
    )
    assert db.query(ReferralEvent).one().status == "credited"


def test_manual_grant_or_missing_student_earns_nothing(db):
    referrer = _profile(db, 50, referral_code="REF50")
    referred = _profile(db, 101)
    referrals.bind_referral(db, referred, referrer)
    db.commit()

    handle(ev.ENROLLMENT_ACTIVATED, {"student_id": 101, "source": "manual"})
    handle(ev.ENROLLMENT_ACTIVATED, {"source": "payhere"})
    handle("something.else", {})
    db.refresh(referrer)
    assert referrer.wallet_credits == 0


def test_consumer_rolls_back_and_reraises(monkeypatch):
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
    with pytest.raises(ValueError):
        handle(ev.AUTH_USER_REGISTERED, {})
    assert fake.rolled_back and fake.closed


def test_consumer_start_binds_user_queue(monkeypatch):
    calls = []
    monkeypatch.setattr(
        event_consumer.messaging, "start_consumer", lambda *args: calls.append(args)
    )
    event_consumer.start()
    assert calls == [("user", "user.queue", ev.QUEUES["user.queue"], handle)]


def test_resync_announces_streams_then_profiles(db, sent):
    db.add(Stream(name="Commerce", subjects=["Accounting"]))
    db.commit()
    _profile(db, 101)
    resync()
    assert [k for k, _ in sent] == [ev.USER_STREAM_UPSERTED, ev.USER_PROFILE_UPSERTED]
    assert sent[0][1]["subjects"] == ["Accounting"]


# ---------------------------------------------------------------------------
# Admin listing and stats
# ---------------------------------------------------------------------------


def test_admin_lists_users_newest_first_with_account_details(db):
    _profile(db, 101, first_name="Ama")
    _profile(db, 102, first_name="Kamal")
    _account(db, 101, "ama@example.com", is_active=False)

    data = client.get("/", headers=ADMIN).json()
    assert data["total"] == 2
    ama = {u["id"]: u for u in data["items"]}[101]
    kamal = {u["id"]: u for u in data["items"]}[102]
    assert [u["id"] for u in data["items"]] == [102, 101]
    assert (ama["email"], ama["is_active"]) == ("ama@example.com", False)
    assert (kamal["email"], kamal["is_active"]) == (None, None)
    assert ama["name"] == "Ama Perera"


def test_admin_list_filters_and_pages(db):
    _profile(db, 101, first_name="Ama")
    _profile(db, 102, first_name="Kamal", last_name="Silva")
    _profile(db, 5, role=ProfileRole.teacher, first_name="Mr", last_name="Fernando")

    def ids(query):
        return [u["id"] for u in client.get(f"/{query}", headers=ADMIN).json()["items"]]

    assert ids("?role=teacher") == [5]
    assert ids("?search=silv") == [102]
    assert ids("?search=perera") == [101]
    assert ids("?page=2&page_size=2") == [101]
    bad = client.get("/?role=parent", headers=ADMIN)
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "INVALID_ROLE"
    assert client.get("/?page_size=101", headers=ADMIN).status_code == 422


def test_admin_only_routes(db):
    for path in ("/", "/stats"):
        assert client.get(path, headers=STUDENT).status_code == 403
        assert client.get(path).status_code == 401


def test_role_stats(db):
    _profile(db, 101)
    _profile(db, 102)
    _profile(db, 5, role=ProfileRole.teacher)
    _profile(db, 1, role=ProfileRole.admin)
    assert client.get("/stats", headers=ADMIN).json() == {
        "total_students": 2,
        "total_teachers": 1,
        "total_admins": 1,
    }


# ---------------------------------------------------------------------------
# Own profile
# ---------------------------------------------------------------------------


def test_create_own_profile_takes_role_and_email_from_the_token(db, sent):
    resp = client.post(
        "/",
        json={"first_name": "Ama", "last_name": "Perera", "mobile_no": "077-123 4567"},
        headers=STUDENT,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert (data["id"], data["role"], data["email"]) == (
        101,
        "student",
        "ama@example.com",
    )
    assert data["mobile_no"] == "0771234567"
    assert sent[-1][0] == ev.USER_PROFILE_UPSERTED


def test_create_profile_twice_is_a_conflict(db):
    _profile(db, 101)
    resp = client.post("/", json={"first_name": "A", "last_name": "B"}, headers=STUDENT)
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
def test_create_profile_validation(extra):
    body = {"first_name": "A", "last_name": "B", **extra}
    assert client.post("/", json=body, headers=STUDENT).status_code == 422


def test_create_profile_with_a_referral_code(db):
    _profile(db, 50, referral_code="REF50")
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
    assert resp.json()["referred_by_user_id"] == 50
    assert db.query(ReferralEvent).one().status == "pending"


def test_unknown_referral_code_is_ignored_at_signup(db):
    resp = client.post(
        "/",
        json={"first_name": "A", "last_name": "B", "referral_code": "NOPE"},
        headers=STUDENT,
    )
    assert resp.status_code == 201 and resp.json()["referred_by_user_id"] is None


def test_read_own_and_other_profiles(db):
    _profile(db, 101)
    _profile(db, 102)
    _account(db, 102, "kamal@example.com")

    own = client.get("/101", headers=STUDENT).json()
    assert own["email"] == "ama@example.com"  # from the caller's own token
    assert client.get("/102", headers=ADMIN).json()["email"] == "kamal@example.com"
    resp = client.get("/102", headers=STUDENT)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "INSUFFICIENT_PERMISSIONS"
    assert (
        client.get("/999", headers=ADMIN).json()["detail"]["error"] == "USER_NOT_FOUND"
    )


def test_own_email_falls_back_to_the_account_copy(db):
    _profile(db, 102)
    _account(db, 102, "kamal@example.com", is_active=True)
    data = client.get("/102", headers=OTHER_STUDENT).json()
    assert (data["email"], data["is_active"]) == ("kamal@example.com", True)


def test_update_own_profile(db, sent):
    _profile(db, 101)
    resp = client.put(
        "/101",
        json={"school": "Royal College", "district": " Colombo ", "grade": 11},
        headers=STUDENT,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert (data["school"], data["district"], data["grade"]) == (
        "Royal College",
        "Colombo",
        11,
    )
    assert sent[-1][1]["school"] == "Royal College"


def test_update_profile_permissions_and_missing(db):
    _profile(db, 102)
    assert client.put("/102", json={}, headers=STUDENT).status_code == 403
    assert client.put("/102", json={"bio": "hi"}, headers=ADMIN).status_code == 200
    assert client.put("/999", json={}, headers=ADMIN).status_code == 404


def test_referral_code_entered_on_profile_edit(db):
    _profile(db, 50, referral_code="REF50")
    me = _profile(db, 101, referral_code="MINE")
    resp = client.put("/101", json={"referral_code": "REF50"}, headers=STUDENT)
    assert resp.json()["referred_by_user_id"] == 50
    assert resp.json()["referral_code"] == "MINE"  # their own code is untouched
    db.refresh(me)
    assert me.referred_by_user_id == 50

    # Entering their own code, or a second code, changes nothing.
    client.put("/101", json={"referral_code": "MINE"}, headers=STUDENT)
    db.refresh(me)
    assert me.referred_by_user_id == 50


def test_admin_changes_role(db, sent):
    _profile(db, 101)
    resp = client.put("/101/role", json={"role": "teacher"}, headers=ADMIN)
    assert resp.json()["role"] == "teacher"
    assert sent[-1][1]["role"] == "teacher"
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


def test_admin_deletes_profile(db, sent):
    _profile(db, 101)
    assert client.delete("/101", headers=ADMIN).status_code == 204
    assert db.query(UserProfile).count() == 0
    assert sent[-1] == (ev.USER_PROFILE_DELETED, {"user_id": 101})
    assert client.delete("/101", headers=ADMIN).status_code == 404


# ---------------------------------------------------------------------------
# Streams and subjects
# ---------------------------------------------------------------------------


def test_stream_crud_and_events(db, sent):
    resp = client.post(
        "/streams",
        json={"name": "Commerce", "subjects": ["Accounting", "Economics"]},
        headers=ADMIN,
    )
    assert resp.status_code == 201
    stream_id = resp.json()["id"]
    assert sent[-1][0] == ev.USER_STREAM_UPSERTED

    resp = client.put(f"/streams/{stream_id}", json={"is_active": False}, headers=ADMIN)
    assert resp.json()["is_active"] is False
    assert sent[-1][1]["is_active"] is False

    assert client.get("/streams").json() == []
    assert [s["name"] for s in client.get("/streams?active_only=false").json()] == [
        "Commerce"
    ]

    assert client.delete(f"/streams/{stream_id}", headers=ADMIN).status_code == 204
    assert sent[-1] == (ev.USER_STREAM_DELETED, {"id": stream_id})


def test_stream_errors(db):
    assert client.put("/streams/9", json={}, headers=ADMIN).status_code == 404
    resp = client.delete("/streams/9", headers=ADMIN)
    assert resp.json()["detail"]["error"] == "STREAM_NOT_FOUND"
    assert client.post("/streams", json={"name": ""}, headers=ADMIN).status_code == 422
    assert (
        client.post("/streams", json={"name": "x"}, headers=TEACHER).status_code == 403
    )


def test_grade_subject_crud(db):
    resp = client.post(
        "/grade-subjects", json={"name": "Science", "grade": 9}, headers=ADMIN
    )
    assert resp.status_code == 201
    subject_id = resp.json()["id"]
    client.post("/grade-subjects", json={"name": "Art", "grade": 9}, headers=ADMIN)
    client.post("/grade-subjects", json={"name": "Maths", "grade": 10}, headers=ADMIN)

    assert [s["name"] for s in client.get("/grade-subjects?grade=9").json()] == [
        "Art",
        "Science",
    ]
    client.put(
        f"/grade-subjects/{subject_id}", json={"is_active": False}, headers=ADMIN
    )
    assert len(client.get("/grade-subjects").json()) == 2
    assert len(client.get("/grade-subjects?active_only=false").json()) == 3

    assert (
        client.delete(f"/grade-subjects/{subject_id}", headers=ADMIN).status_code == 204
    )


def test_grade_subject_errors(db):
    assert (
        client.post(
            "/grade-subjects", json={"name": "Physics", "grade": 12}, headers=ADMIN
        ).status_code
        == 422
    )
    assert client.put("/grade-subjects/9", json={}, headers=ADMIN).status_code == 404
    resp = client.delete("/grade-subjects/9", headers=ADMIN)
    assert resp.json()["detail"]["error"] == "SUBJECT_NOT_FOUND"


def test_subjects_for_a_grade(db):
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


def test_referral_summary(db, monkeypatch):
    monkeypatch.setattr(referrals, "FRONTEND_URL", "https://edura.lk/")
    _profile(db, 101, referral_code="AMA1", wallet_credits=3)
    data = client.get("/referral/summary", headers=STUDENT).json()
    assert data["referral_link"] == "https://edura.lk/register?ref=AMA1"
    assert data["wallet_credits"] == 3
    resp = client.get("/referral/summary", headers=OTHER_STUDENT)
    assert resp.json()["detail"]["error"] == "PROFILE_NOT_FOUND"


def test_referral_events_list(db):
    me = _profile(db, 101)
    for uid, school in ((201, "Royal"), (202, None)):
        friend = _profile(db, uid, first_name=f"F{uid}", last_name="X", school=school)
        referrals.bind_referral(db, friend, me)
    db.commit()
    referrals.credit_on_enrollment(db, 201)

    events = client.get("/referral/events", headers=STUDENT).json()["events"]
    by_id = {e["referred_student_id"]: e for e in events}
    assert by_id["201"]["status"] == "credited" and by_id["201"]["credited_at"]
    assert by_id["202"]["status"] == "pending" and by_id["202"]["credited_at"] is None
    assert by_id["201"]["referred_student_name"] == "F201 X"
    assert (
        len(client.get("/referral/events?limit=1", headers=STUDENT).json()["events"])
        == 1
    )
    assert client.get("/referral/events?limit=0", headers=STUDENT).status_code == 422


def test_apply_referral_code(db):
    _profile(db, 50, referral_code="REF50")
    _profile(db, 101)
    resp = client.post(
        "/referral/apply", json={"referral_code": "ref50"}, headers=STUDENT
    )
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
def test_apply_referral_code_errors(db, setup, code, status, error):
    _profile(db, 50, referral_code="REF50")
    role = ProfileRole.teacher if setup == "teacher_profile" else ProfileRole.student
    _profile(
        db, 101, role=role, referred_by_user_id=50 if setup == "referred" else None
    )
    if setup == "event_exists":
        db.add(ReferralEvent(referrer_user_id=7, referred_user_id=101))
        db.commit()

    resp = client.post("/referral/apply", json={"referral_code": code}, headers=STUDENT)
    assert resp.status_code == status
    assert resp.json()["detail"]["error"] == error


def test_apply_needs_a_student_with_a_profile(db):
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


def test_credit_needs_a_pending_event_and_a_referrer(db):
    assert referrals.credit_on_enrollment(db, 101) is False
    db.add(ReferralEvent(referrer_user_id=404, referred_user_id=101, status="pending"))
    db.commit()
    assert referrals.credit_on_enrollment(db, 101) is False  # referrer was deleted


def test_bind_referral_refuses_self_and_second_referrals(db):
    a = _profile(db, 101)
    b = _profile(db, 102, referred_by_user_id=50)
    assert referrals.bind_referral(db, a, a) is None
    assert referrals.bind_referral(db, b, a) is None


def test_find_referrer_ignores_blank_codes(db):
    assert referrals.find_referrer(db, None) is None
    assert referrals.find_referrer(db, "   ") is None


# ---------------------------------------------------------------------------
# profile_service helpers
# ---------------------------------------------------------------------------


def test_referral_code_prefix(db):
    assert profile_service.generate_referral_code(db, "o'Neil").startswith("ONEI")
    assert profile_service.generate_referral_code(db, "123").startswith("USER")
    assert len(profile_service.generate_referral_code(db, "Al")) == 6


def test_referral_code_falls_back_after_collisions(db, monkeypatch):
    # With every random character forced to "A", each try produces "AMA" + "AAAA".
    _profile(db, 101, referral_code="AMAAAAA")
    monkeypatch.setattr(profile_service.secrets, "choice", lambda alphabet: "A")
    code = profile_service.generate_referral_code(db, "Ama")
    assert code != "AMAAAAA" and len(code) == 12  # token_hex(6)


def test_profile_event_payload(db):
    p = _profile(db, 101, first_name="Ama", last_name="")
    payload = profile_service.profile_event(p)
    assert payload["full_name"] == "Ama" and payload["role"] == "student"
    p.created_at = None
    assert profile_service.profile_event(p)["created_at"] is None


def test_stream_event_defaults():
    payload = profile_service.stream_event(Stream(id=1, name="X", subjects=None))
    assert payload["subjects"] == []
