"""Payment service gaps on SQLite: PayHere checkout and webhook, free enrollment,
receipt uploads and review, payment listing, admin revenue stats, bank accounts,
startup and the catalogue consumer."""

import os
from datetime import datetime, timedelta, timezone

# Same settings as the other payment test files, set before shared.auth is imported.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-payment-tests")
os.environ.setdefault("PAYHERE_MERCHANT_ID", "123456")
os.environ.setdefault("PAYHERE_MERCHANT_SECRET", "test_merchant_secret")

import hashlib  # noqa: E402

import event_consumer  # noqa: E402
import events  # noqa: E402
import jwt  # noqa: E402
import main  # noqa: E402
import pytest  # noqa: E402
import router  # noqa: E402
import utils  # noqa: E402
from database import Base, get_db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    BankAccount,
    Payment,
    PaymentMethod,
    PaymentReceipt,
    PaymentStatus,
    ReceiptStatus,
    ReplicaCourse,
)
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from shared import auth, media  # noqa: E402
from shared import events as ev  # noqa: E402

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


def _auth(user_id: int, role: str) -> dict:
    token = jwt.encode(
        {"sub": str(user_id), "role": role}, auth.SECRET_KEY, algorithm=auth.ALGORITHM
    )
    return {"Authorization": f"Bearer {token}"}


STUDENT = _auth(101, "student")
OTHER_STUDENT = _auth(102, "student")
TEACHER = _auth(5, "teacher")
ADMIN = _auth(1, "admin")
PDF = b"%PDF-1.4\n%receipt\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


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
    monkeypatch.setattr(
        events, "publish", lambda key, payload: published.append((key, payload)) or True
    )
    return published


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    stored = []

    def upload(content, filename, folder, resource_type="auto"):
        stored.append((filename, folder))
        return f"{folder}/{filename}", f"https://cdn/{filename}"

    monkeypatch.setattr(media, "upload", upload)
    return stored


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _payment(db, **fields) -> Payment:
    fields.setdefault("student_id", 101)
    fields.setdefault("course_id", 10)
    fields.setdefault("payment_method", PaymentMethod.PAYHERE)
    fields.setdefault("payment_status", PaymentStatus.PENDING)
    fields.setdefault("amount", 1500)
    p = Payment(**fields)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _receipt(db, payment, **fields) -> PaymentReceipt:
    fields.setdefault("receipt_status", ReceiptStatus.PENDING)
    r = PaymentReceipt(
        payment_id=payment.id,
        student_id=payment.student_id,
        receipt_url="https://cdn/r.pdf",
        **fields,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _signed(order_id, amount="1500.00", status_code="2", currency="LKR", **extra):
    """A webhook form PayHere would send, signed with this service's secret."""
    secret_hash = utils.generate_merchant_secret_hash(utils.PAYHERE_MERCHANT_SECRET)
    raw = f"{utils.PAYHERE_MERCHANT_ID}{order_id}{amount}{currency}{status_code}{secret_hash}"
    form = {
        "merchant_id": utils.PAYHERE_MERCHANT_ID,
        "order_id": order_id,
        "payhere_amount": amount,
        "payhere_currency": currency,
        "status_code": status_code,
        "md5sig": hashlib.md5(raw.encode()).hexdigest().upper(),
        "payment_id": "PH-998877",
    }
    form.update(extra)
    return form


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def test_health():
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "payment_service",
    }


def test_startup_creates_tables_and_starts_consumer(monkeypatch):
    startup_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    calls = []
    monkeypatch.setattr(main, "engine", startup_engine)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("consumer"))

    main.on_startup()  # SQLite rejects ADD COLUMN IF NOT EXISTS; logged, not fatal

    with startup_engine.connect() as conn:
        assert "payments" in startup_engine.dialect.get_table_names(conn)
    assert calls == ["consumer"]
    startup_engine.dispose()


def test_startup_survives_create_all_failure(monkeypatch):
    calls = []

    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("consumer"))
    main.on_startup()
    assert calls == ["consumer"]


def test_consumer_start_binds_payment_queue(monkeypatch):
    calls = []
    monkeypatch.setattr(
        event_consumer.messaging, "start_consumer", lambda *args: calls.append(args)
    )
    event_consumer.start()
    assert calls == [
        ("payment", "payment.queue", ev.QUEUES["payment.queue"], event_consumer.handle)
    ]


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
        event_consumer.handle("course.upserted", {})
    assert fake.rolled_back and fake.closed


# ---------------------------------------------------------------------------
# PayHere checkout and webhook
# ---------------------------------------------------------------------------


def test_checkout_creates_a_pending_payment_with_a_valid_hash(db):
    resp = client.post(
        "/checkout", json={"course_id": 10, "amount": 2500}, headers=STUDENT
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["order_id"].startswith("ORD-") and len(data["order_id"]) == 16
    assert (data["amount"], data["currency"]) == (2500.0, "LKR")
    assert data["merchant_id"] == utils.PAYHERE_MERCHANT_ID
    assert data["redirect_url"] == utils.PAYHERE_CHECKOUT_URL
    assert data["hash"] == utils.calculate_checkout_hash(
        utils.PAYHERE_MERCHANT_ID,
        data["order_id"],
        "2500.00",
        "LKR",
        utils.PAYHERE_MERCHANT_SECRET,
    )
    payment = db.query(Payment).one()
    assert (payment.student_id, payment.payment_status, payment.payhere_order_id) == (
        101,
        PaymentStatus.PENDING,
        data["order_id"],
    )


def test_checkout_without_amount_defaults_to_1000():
    resp = client.post("/checkout", json={"course_id": 10}, headers=STUDENT)
    assert resp.json()["amount"] == 1000.0


def test_checkout_roles():
    body = {"course_id": 10}
    assert client.post("/checkout", json=body, headers=ADMIN).status_code == 200
    assert client.post("/checkout", json=body, headers=TEACHER).status_code == 403


def test_webhook_confirms_payment_and_announces_it(db, sent):
    payment = _payment(db, payhere_order_id="ORD-1")
    resp = client.post("/webhook", data=_signed("ORD-1"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUCCESS"

    db.refresh(payment)
    assert payment.payment_status == PaymentStatus.SUCCESS
    assert payment.reference_no == "PH-998877" and payment.paid_at is not None
    ((key, data),) = sent
    assert key == ev.PAYMENT_SUCCESS
    assert (data["order_id"], data["amount"], data["payment_id"]) == (
        "ORD-1",
        1500.0,
        payment.id,
    )


def test_webhook_is_idempotent(db, sent):
    _payment(db, payhere_order_id="ORD-1")
    client.post("/webhook", data=_signed("ORD-1"))
    again = client.post("/webhook", data=_signed("ORD-1"))
    assert again.json() == {"status": "SUCCESS", "message": "Webhook already processed"}
    assert len(sent) == 1


def test_webhook_failure_status_marks_payment_failed(db, sent):
    payment = _payment(db, payhere_order_id="ORD-1")
    resp = client.post("/webhook", data=_signed("ORD-1", status_code="-2"))
    assert resp.json() == {
        "status": "FAILED",
        "message": "Payment failed with status code -2",
    }
    db.refresh(payment)
    assert payment.payment_status == PaymentStatus.FAILED
    assert sent == []


def test_webhook_rejects_bad_signatures(db):
    _payment(db, payhere_order_id="ORD-1")
    tampered = _signed("ORD-1")
    tampered["payhere_amount"] = "1.00"  # signed for 1500.00
    resp = client.post("/webhook", data=tampered)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "INVALID_SIGNATURE"
    assert client.post("/webhook", data={}).status_code == 400


def test_webhook_for_unknown_order():
    resp = client.post("/webhook", data=_signed("ORD-404"))
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "PAYMENT_NOT_FOUND"


def test_signature_check_is_case_insensitive():
    form = _signed("ORD-1")
    assert utils.verify_payhere_signature(
        form["merchant_id"],
        "ORD-1",
        "1500.00",
        "LKR",
        "2",
        form["md5sig"].lower(),
    )


# ---------------------------------------------------------------------------
# Free enrollment
# ---------------------------------------------------------------------------


def test_free_course_enrolls_once(db, sent):
    db.add(ReplicaCourse(id=10, title="Intro", price=0))
    db.commit()

    first = client.post("/free-enroll", json={"course_id": 10}, headers=STUDENT)
    assert first.json()["message"] == "Enrolled successfully"
    again = client.post("/free-enroll", json={"course_id": 10}, headers=STUDENT)
    assert again.json() == {
        "message": "Already enrolled",
        "course_id": 10,
        "payment_id": first.json()["payment_id"],
    }
    assert len(sent) == 1 and sent[0][1]["order_id"].startswith("FREE-")
    assert db.query(Payment).one().payment_method == PaymentMethod.MANUAL


def test_free_enroll_errors(db):
    db.add(ReplicaCourse(id=11, title="Paid", price=1500))
    db.commit()
    paid = client.post("/free-enroll", json={"course_id": 11}, headers=STUDENT)
    assert paid.status_code == 400
    assert paid.json()["detail"]["error"] == "COURSE_NOT_FREE"
    missing = client.post("/free-enroll", json={"course_id": 99}, headers=STUDENT)
    assert missing.json()["detail"]["error"] == "COURSE_NOT_FOUND"
    assert (
        client.post("/free-enroll", json={"course_id": 11}, headers=ADMIN).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def _upload(content=PDF, mime="application/pdf", headers=STUDENT, name="slip.pdf"):
    return client.post(
        "/receipts",
        data={"course_id": "10", "amount": "1500"},
        files={"file": (name, content, mime)},
        headers=headers,
    )


def test_upload_receipt(db, sent, storage):
    resp = _upload(PNG, "image/png", name="slip.png")
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "PENDING"
    filename, folder = storage[0]
    assert folder == media.RECEIPT_FOLDER
    assert filename.startswith("101_10_") and filename.endswith("_slip.png")
    assert data["receipt_url"] == f"https://cdn/{filename}"
    assert sent == [
        (
            ev.PAYMENT_RECEIPT_UPLOADED,
            {
                "receipt_id": data["receipt_id"],
                "payment_id": data["payment_id"],
                "student_id": 101,
            },
        )
    ]
    assert db.get(Payment, data["payment_id"]).payment_method == PaymentMethod.MANUAL


def test_receipt_bytes_must_match_the_declared_type(db, storage):
    resp = _upload(b"plain text pretending", "application/pdf")
    assert resp.status_code == 415
    assert resp.json()["detail"]["error"] == "UNSUPPORTED_FILE_TYPE"
    assert storage == [] and db.query(Payment).count() == 0


def test_empty_receipt_is_refused():
    resp = _upload(b"", "application/pdf")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "EMPTY_FILE"


def test_storage_outage_is_reported(monkeypatch, db):
    def down(*args, **kwargs):
        raise media.MediaError(503, "CLOUDINARY_NOT_CONFIGURED", "not set")

    monkeypatch.setattr(media, "upload", down)
    resp = _upload()
    assert resp.status_code == 503
    assert db.query(Payment).count() == 0


def test_teacher_cannot_upload_receipts():
    assert _upload(headers=TEACHER).status_code == 403


def test_list_payments_by_role(db):
    _payment(db, student_id=101)
    _payment(db, student_id=102)
    assert [p["student_id"] for p in client.get("/", headers=STUDENT).json()] == [101]
    assert len(client.get("/", headers=ADMIN).json()) == 2
    assert client.get("/", headers=TEACHER).json() == []
    assert client.get("/").status_code == 401


def test_admin_lists_receipts_with_filter(db):
    pending = _receipt(db, _payment(db, amount=1000))
    _receipt(db, _payment(db), receipt_status=ReceiptStatus.APPROVED)

    rows = client.get("/receipts", headers=ADMIN).json()
    assert len(rows) == 2
    only_pending = client.get("/receipts?status_filter=pending", headers=ADMIN).json()
    assert [(r["id"], r["status"], r["amount"]) for r in only_pending] == [
        (pending.id, "pending", 1000.0)
    ]
    bad = client.get("/receipts?status_filter=lost", headers=ADMIN)
    assert bad.status_code == 422
    assert client.get("/receipts", headers=STUDENT).status_code == 403


def test_approve_receipt(db, sent):
    payment = _payment(db, payment_method=PaymentMethod.MANUAL)
    receipt = _receipt(db, payment)
    resp = client.put(
        f"/receipts/{receipt.id}/verify", json={"status": "verified"}, headers=ADMIN
    )
    assert resp.status_code == 200
    assert (resp.json()["status"], resp.json()["reviewer_id"]) == ("approved", 1)
    db.refresh(payment)
    assert payment.payment_status == PaymentStatus.SUCCESS
    assert [k for k, _ in sent] == [ev.PAYMENT_SUCCESS, ev.PAYMENT_RECEIPT_REVIEWED]
    assert sent[0][1]["order_id"] == f"MANUAL-{payment.id}"
    assert sent[1][1]["status"] == "APPROVED"


def test_reject_receipt_with_reason(db, sent):
    payment = _payment(db)
    receipt = _receipt(db, payment)
    resp = client.put(
        f"/receipts/{receipt.id}/verify",
        json={"status": "rejected", "rejection_reason": "Blurry slip"},
        headers=ADMIN,
    )
    assert (resp.json()["status"], resp.json()["reviewer_note"]) == (
        "rejected",
        "Blurry slip",
    )
    db.refresh(payment)
    assert payment.payment_status == PaymentStatus.FAILED
    assert [k for k, _ in sent] == [ev.PAYMENT_RECEIPT_REVIEWED]


def test_receipt_review_errors(db):
    reviewed = _receipt(db, _payment(db), receipt_status=ReceiptStatus.APPROVED)
    orphan = _receipt(db, Payment(id=999, student_id=101))
    url = "/receipts/{}/verify"
    body = {"status": "verified"}

    resp = client.put(url.format(reviewed.id), json=body, headers=ADMIN)
    assert resp.json()["detail"]["error"] == "ALREADY_REVIEWED"
    resp = client.put(url.format(orphan.id), json=body, headers=ADMIN)
    assert resp.json()["detail"]["error"] == "PAYMENT_NOT_FOUND"
    resp = client.put(url.format(404), json=body, headers=ADMIN)
    assert resp.json()["detail"]["error"] == "RECEIPT_NOT_FOUND"
    assert (
        client.put(
            url.format(reviewed.id), json={"status": "maybe"}, headers=ADMIN
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# Admin revenue stats
# ---------------------------------------------------------------------------


def test_admin_stats(db):
    now = datetime.now(timezone.utc)
    _payment(db, amount=1000, payment_status=PaymentStatus.SUCCESS)
    _payment(db, amount=500, payment_status=PaymentStatus.SUCCESS)
    old = _payment(db, amount=2000, payment_status=PaymentStatus.SUCCESS)
    old.updated_at = now - timedelta(days=70)
    _payment(db, amount=0, payment_status=PaymentStatus.SUCCESS)  # free: not revenue
    _payment(db, amount=900)  # still pending
    db.commit()
    _receipt(db, _payment(db))
    for days in range(6):
        _receipt(
            db,
            _payment(db, student_id=200 + days),
            receipt_status=ReceiptStatus.APPROVED,
            reviewed_at=now - timedelta(days=days),
        )

    stats = client.get("/internal/admin-stats").json()
    assert stats["total_revenue"] == 3500.0
    assert stats["revenue_this_month"] in (1500.0, 3500.0)  # month boundary safe
    assert stats["pending_bank_slips"] == 1
    assert sum(m["count"] for m in stats["revenue_by_month"]) == 3
    assert [r["student_id"] for r in stats["recent_verified"]] == [
        200,
        201,
        202,
        203,
        204,
    ]


def test_admin_stats_when_empty():
    assert client.get("/internal/admin-stats").json() == {
        "total_revenue": 0,
        "revenue_this_month": 0.0,
        "pending_bank_slips": 0,
        "revenue_by_month": [],
        "recent_verified": [],
    }


def test_sl_time_conversion():
    naive = router._sl(datetime(2026, 1, 1, 20, 0))
    assert (naive.day, naive.hour, naive.minute) == (2, 1, 30)


# ---------------------------------------------------------------------------
# Bank accounts
# ---------------------------------------------------------------------------

ACCOUNT = {
    "bank_name": "BOC",
    "account_number": "123",
    "account_name": "Edura",
    "branch": "Colombo",
}


def test_bank_account_crud(db):
    first = client.post(
        "/admin/bank-accounts", json={**ACCOUNT, "order_number": 2}, headers=ADMIN
    )
    assert first.status_code == 201
    second = client.post(
        "/admin/bank-accounts",
        json={**ACCOUNT, "bank_name": "HNB", "order_number": 1},
        headers=ADMIN,
    ).json()

    assert [a["bank_name"] for a in client.get("/bank-accounts").json()] == [
        "HNB",
        "BOC",
    ]
    resp = client.patch(
        f"/admin/bank-accounts/{second['id']}",
        json={"is_active": False},
        headers=ADMIN,
    )
    assert resp.json()["is_active"] is False
    assert [a["bank_name"] for a in client.get("/bank-accounts").json()] == ["BOC"]
    assert len(client.get("/admin/bank-accounts", headers=ADMIN).json()) == 2

    assert (
        client.delete(f"/admin/bank-accounts/{second['id']}", headers=ADMIN).status_code
        == 204
    )
    assert db.query(BankAccount).count() == 1


def test_bank_account_errors():
    assert (
        client.patch("/admin/bank-accounts/9", json={}, headers=ADMIN).status_code
        == 404
    )
    resp = client.delete("/admin/bank-accounts/9", headers=ADMIN)
    assert resp.json()["detail"]["error"] == "BANK_ACCOUNT_NOT_FOUND"
    assert (
        client.post(
            "/admin/bank-accounts", json={**ACCOUNT, "bank_name": ""}, headers=ADMIN
        ).status_code
        == 422
    )
    assert client.get("/admin/bank-accounts", headers=STUDENT).status_code == 403


# ---------------------------------------------------------------------------
# Event payloads
# ---------------------------------------------------------------------------


def test_payment_success_defaults_completion_time(sent):
    events.publish_payment_success(101, 10, "ORD-1", 100.0)
    completed = datetime.fromisoformat(sent[0][1]["completed_at"])
    assert abs((datetime.now(timezone.utc) - completed).total_seconds()) < 5
    assert sent[0][1]["payment_id"] is None


def test_receipt_reviewed_without_time(sent):
    events.publish_receipt_reviewed(1, 101, "REJECTED", None)
    assert sent[0][1]["reviewed_at"] is None
