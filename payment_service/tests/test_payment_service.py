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


def test_given_running_service_when_health_requested_then_reports_ok():
    # Act and assert
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "payment_service",
    }


def test_given_empty_database_when_service_starts_then_tables_created_and_consumer_started(
    monkeypatch,
):
    # Arrange
    startup_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    calls = []
    monkeypatch.setattr(main, "engine", startup_engine)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("consumer"))

    main.on_startup()  # SQLite rejects ADD COLUMN IF NOT EXISTS; logged, not fatal

    # Act and assert
    with startup_engine.connect() as conn:
        assert "payments" in startup_engine.dialect.get_table_names(conn)

    # Act and assert
    assert calls == ["consumer"]
    startup_engine.dispose()


def test_given_table_creation_fails_when_service_starts_then_consumer_still_starts(
    monkeypatch,
):
    # Arrange
    calls = []

    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("consumer"))

    # Act
    main.on_startup()

    # Assert
    assert calls == ["consumer"]


def test_given_consumer_when_started_then_binds_payment_queue(monkeypatch):
    # Arrange
    calls = []
    monkeypatch.setattr(
        event_consumer.messaging, "start_consumer", lambda *args: calls.append(args)
    )

    # Act
    event_consumer.start()

    # Assert
    assert calls == [
        ("payment", "payment.queue", ev.QUEUES["payment.queue"], event_consumer.handle)
    ]


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
        event_consumer.handle("course.upserted", {})

    # Act and assert
    assert fake.rolled_back and fake.closed


# ---------------------------------------------------------------------------
# PayHere checkout and webhook
# ---------------------------------------------------------------------------


def test_given_student_when_checkout_then_pending_payment_created_with_valid_hash(db):
    # Act
    resp = client.post(
        "/checkout", json={"course_id": 10, "amount": 2500}, headers=STUDENT
    )

    # Assert
    assert resp.status_code == 200

    data = resp.json()

    # Act and assert
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

    # Act
    payment = db.query(Payment).one()

    # Assert
    assert (payment.student_id, payment.payment_status, payment.payhere_order_id) == (
        101,
        PaymentStatus.PENDING,
        data["order_id"],
    )


def test_given_no_amount_when_checkout_then_amount_defaults_to_1000():
    # Act
    resp = client.post("/checkout", json={"course_id": 10}, headers=STUDENT)

    # Assert
    assert resp.json()["amount"] == 1000.0


def test_given_admin_and_teacher_when_checkout_then_admin_allowed_and_teacher_forbidden():
    # Arrange
    body = {"course_id": 10}

    # Act and assert
    assert client.post("/checkout", json=body, headers=ADMIN).status_code == 200
    assert client.post("/checkout", json=body, headers=TEACHER).status_code == 403


def test_given_pending_payment_when_valid_webhook_then_payment_confirmed_and_announced(
    db, sent
):
    # Arrange
    payment = _payment(db, payhere_order_id="ORD-1")

    # Act
    resp = client.post("/webhook", data=_signed("ORD-1"))

    # Assert
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUCCESS"

    db.refresh(payment)

    # Act and assert
    assert payment.payment_status == PaymentStatus.SUCCESS
    assert payment.reference_no == "PH-998877" and payment.paid_at is not None
    ((key, data),) = sent

    # Act and assert
    assert key == ev.PAYMENT_SUCCESS
    assert (data["order_id"], data["amount"], data["payment_id"]) == (
        "ORD-1",
        1500.0,
        payment.id,
    )


def test_given_processed_webhook_when_sent_again_then_not_announced_twice(db, sent):
    # Arrange
    _payment(db, payhere_order_id="ORD-1")
    client.post("/webhook", data=_signed("ORD-1"))

    # Act
    again = client.post("/webhook", data=_signed("ORD-1"))

    # Assert
    assert again.json() == {"status": "SUCCESS", "message": "Webhook already processed"}
    assert len(sent) == 1


def test_given_failure_status_when_webhook_then_payment_marked_failed(db, sent):
    # Arrange
    payment = _payment(db, payhere_order_id="ORD-1")

    # Act
    resp = client.post("/webhook", data=_signed("ORD-1", status_code="-2"))

    # Assert
    assert resp.json() == {
        "status": "FAILED",
        "message": "Payment failed with status code -2",
    }
    db.refresh(payment)

    # Act and assert
    assert payment.payment_status == PaymentStatus.FAILED
    assert sent == []


def test_given_tampered_or_empty_webhook_when_received_then_400_invalid_signature(db):
    # Arrange
    _payment(db, payhere_order_id="ORD-1")
    tampered = _signed("ORD-1")
    tampered["payhere_amount"] = "1.00"  # signed for 1500.00

    # Act
    resp = client.post("/webhook", data=tampered)

    # Assert
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "INVALID_SIGNATURE"

    # Act and assert
    assert client.post("/webhook", data={}).status_code == 400


def test_given_unknown_order_when_webhook_then_404_payment_not_found():
    # Act
    resp = client.post("/webhook", data=_signed("ORD-404"))

    # Assert
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "PAYMENT_NOT_FOUND"


def test_given_lowercase_signature_when_verified_then_accepted():
    # Arrange
    form = _signed("ORD-1")

    # Act and assert
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


def test_given_free_course_when_student_enrolls_twice_then_enrolled_once(db, sent):
    # Arrange
    db.add(ReplicaCourse(id=10, title="Intro", price=0))
    db.commit()

    # Act
    first = client.post("/free-enroll", json={"course_id": 10}, headers=STUDENT)

    # Assert
    assert first.json()["message"] == "Enrolled successfully"

    # Act
    again = client.post("/free-enroll", json={"course_id": 10}, headers=STUDENT)

    # Assert
    assert again.json() == {
        "message": "Already enrolled",
        "course_id": 10,
        "payment_id": first.json()["payment_id"],
    }
    assert len(sent) == 1 and sent[0][1]["order_id"].startswith("FREE-")
    assert db.query(Payment).one().payment_method == PaymentMethod.MANUAL


def test_given_paid_missing_course_or_admin_when_free_enroll_then_each_rejected(db):
    # Arrange
    db.add(ReplicaCourse(id=11, title="Paid", price=1500))
    db.commit()

    # Act
    paid = client.post("/free-enroll", json={"course_id": 11}, headers=STUDENT)

    # Assert
    assert paid.status_code == 400
    assert paid.json()["detail"]["error"] == "COURSE_NOT_FREE"

    # Act
    missing = client.post("/free-enroll", json={"course_id": 99}, headers=STUDENT)

    # Assert
    assert missing.json()["detail"]["error"] == "COURSE_NOT_FOUND"

    # Act and assert
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


def test_given_valid_image_when_receipt_uploaded_then_pending_receipt_stored_and_announced(
    db, sent, storage
):
    # Act
    resp = _upload(PNG, "image/png", name="slip.png")

    # Assert
    assert resp.status_code == 201

    # Act
    data = resp.json()

    # Assert
    assert data["status"] == "PENDING"
    filename, folder = storage[0]

    # Act and assert
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

    # Act and assert
    assert db.get(Payment, data["payment_id"]).payment_method == PaymentMethod.MANUAL


def test_given_bytes_not_matching_declared_type_when_receipt_uploaded_then_415_and_nothing_stored(
    db, storage
):
    # Act
    resp = _upload(b"plain text pretending", "application/pdf")

    # Assert
    assert resp.status_code == 415
    assert resp.json()["detail"]["error"] == "UNSUPPORTED_FILE_TYPE"
    assert storage == [] and db.query(Payment).count() == 0


def test_given_empty_file_when_receipt_uploaded_then_422_empty_file():
    # Act
    resp = _upload(b"", "application/pdf")

    # Assert
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "EMPTY_FILE"


def test_given_storage_outage_when_receipt_uploaded_then_503_and_no_payment(
    monkeypatch, db
):
    # Arrange
    def down(*args, **kwargs):
        raise media.MediaError(503, "CLOUDINARY_NOT_CONFIGURED", "not set")

    monkeypatch.setattr(media, "upload", down)

    # Act
    resp = _upload()

    # Assert
    assert resp.status_code == 503
    assert db.query(Payment).count() == 0


def test_given_teacher_when_receipt_uploaded_then_403():
    # Act and assert
    assert _upload(headers=TEACHER).status_code == 403


def test_given_payments_of_two_students_when_listed_then_each_role_sees_its_own_scope(
    db,
):
    # Arrange
    _payment(db, student_id=101)
    _payment(db, student_id=102)

    # Act and assert
    assert [p["student_id"] for p in client.get("/", headers=STUDENT).json()] == [101]
    assert len(client.get("/", headers=ADMIN).json()) == 2
    assert client.get("/", headers=TEACHER).json() == []
    assert client.get("/").status_code == 401


def test_given_pending_and_approved_receipts_when_admin_lists_then_filter_and_access_rules_apply(
    db,
):
    # Arrange
    pending = _receipt(db, _payment(db, amount=1000))
    _receipt(db, _payment(db), receipt_status=ReceiptStatus.APPROVED)

    # Act
    rows = client.get("/receipts", headers=ADMIN).json()

    # Assert
    assert len(rows) == 2

    # Act
    only_pending = client.get("/receipts?status_filter=pending", headers=ADMIN).json()

    # Assert
    assert [(r["id"], r["status"], r["amount"]) for r in only_pending] == [
        (pending.id, "pending", 1000.0)
    ]

    # Act
    bad = client.get("/receipts?status_filter=lost", headers=ADMIN)

    # Assert
    assert bad.status_code == 422

    # Act and assert
    assert client.get("/receipts", headers=STUDENT).status_code == 403


def test_given_pending_receipt_when_admin_approves_then_payment_succeeds_and_events_published(
    db, sent
):
    # Arrange
    payment = _payment(db, payment_method=PaymentMethod.MANUAL)
    receipt = _receipt(db, payment)

    # Act
    resp = client.put(
        f"/receipts/{receipt.id}/verify", json={"status": "verified"}, headers=ADMIN
    )

    # Assert
    assert resp.status_code == 200
    assert (resp.json()["status"], resp.json()["reviewer_id"]) == ("approved", 1)
    db.refresh(payment)

    # Act and assert
    assert payment.payment_status == PaymentStatus.SUCCESS
    assert [k for k, _ in sent] == [ev.PAYMENT_SUCCESS, ev.PAYMENT_RECEIPT_REVIEWED]
    assert sent[0][1]["order_id"] == f"MANUAL-{payment.id}"
    assert sent[1][1]["status"] == "APPROVED"


def test_given_pending_receipt_when_admin_rejects_with_reason_then_payment_failed_and_review_announced(
    db, sent
):
    # Arrange
    payment = _payment(db)
    receipt = _receipt(db, payment)

    # Act
    resp = client.put(
        f"/receipts/{receipt.id}/verify",
        json={"status": "rejected", "rejection_reason": "Blurry slip"},
        headers=ADMIN,
    )

    # Assert
    assert (resp.json()["status"], resp.json()["reviewer_note"]) == (
        "rejected",
        "Blurry slip",
    )
    db.refresh(payment)

    # Act and assert
    assert payment.payment_status == PaymentStatus.FAILED
    assert [k for k, _ in sent] == [ev.PAYMENT_RECEIPT_REVIEWED]


def test_given_reviewed_orphan_or_unknown_receipt_when_reviewed_then_each_error_returned(
    db,
):
    # Arrange
    reviewed = _receipt(db, _payment(db), receipt_status=ReceiptStatus.APPROVED)
    orphan = _receipt(db, Payment(id=999, student_id=101))
    url = "/receipts/{}/verify"
    body = {"status": "verified"}

    # Act
    resp = client.put(url.format(reviewed.id), json=body, headers=ADMIN)

    # Assert
    assert resp.json()["detail"]["error"] == "ALREADY_REVIEWED"

    # Act
    resp = client.put(url.format(orphan.id), json=body, headers=ADMIN)

    # Assert
    assert resp.json()["detail"]["error"] == "PAYMENT_NOT_FOUND"

    # Act
    resp = client.put(url.format(404), json=body, headers=ADMIN)

    # Assert
    assert resp.json()["detail"]["error"] == "RECEIPT_NOT_FOUND"

    # Act and assert
    assert (
        client.put(
            url.format(reviewed.id), json={"status": "maybe"}, headers=ADMIN
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# Admin revenue stats
# ---------------------------------------------------------------------------


def test_given_payments_and_receipts_when_admin_stats_requested_then_revenue_figures_computed(
    db,
):
    # Arrange
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

    # Act
    stats = client.get("/internal/admin-stats").json()

    # Assert
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


def test_given_no_payments_when_admin_stats_requested_then_zeroes_returned():
    # Act and assert
    assert client.get("/internal/admin-stats").json() == {
        "total_revenue": 0,
        "revenue_this_month": 0.0,
        "pending_bank_slips": 0,
        "revenue_by_month": [],
        "recent_verified": [],
    }


def test_given_naive_utc_time_when_converted_then_sri_lanka_time_returned():
    # Act
    naive = router._sl(datetime(2026, 1, 1, 20, 0))

    # Assert
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


def test_given_bank_accounts_when_created_updated_and_deleted_then_listings_reflect_changes(
    db,
):
    # Act
    first = client.post(
        "/admin/bank-accounts", json={**ACCOUNT, "order_number": 2}, headers=ADMIN
    )

    # Assert
    assert first.status_code == 201
    second = client.post(
        "/admin/bank-accounts",
        json={**ACCOUNT, "bank_name": "HNB", "order_number": 1},
        headers=ADMIN,
    ).json()

    # Act and assert
    assert [a["bank_name"] for a in client.get("/bank-accounts").json()] == [
        "HNB",
        "BOC",
    ]

    # Act
    resp = client.patch(
        f"/admin/bank-accounts/{second['id']}",
        json={"is_active": False},
        headers=ADMIN,
    )

    # Assert
    assert resp.json()["is_active"] is False

    # Act and assert
    assert [a["bank_name"] for a in client.get("/bank-accounts").json()] == ["BOC"]
    assert len(client.get("/admin/bank-accounts", headers=ADMIN).json()) == 2

    assert (
        client.delete(f"/admin/bank-accounts/{second['id']}", headers=ADMIN).status_code
        == 204
    )

    # Act and assert
    assert db.query(BankAccount).count() == 1


def test_given_missing_account_invalid_name_or_student_when_managing_accounts_then_errors_returned():
    # Act and assert
    assert (
        client.patch("/admin/bank-accounts/9", json={}, headers=ADMIN).status_code
        == 404
    )

    # Act
    resp = client.delete("/admin/bank-accounts/9", headers=ADMIN)

    # Assert
    assert resp.json()["detail"]["error"] == "BANK_ACCOUNT_NOT_FOUND"

    # Act and assert
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


def test_given_no_completion_time_when_payment_success_published_then_defaults_to_now(
    sent,
):
    # Arrange
    events.publish_payment_success(101, 10, "ORD-1", 100.0)

    completed = datetime.fromisoformat(sent[0][1]["completed_at"])

    # Act and assert
    assert abs((datetime.now(timezone.utc) - completed).total_seconds()) < 5
    assert sent[0][1]["payment_id"] is None


def test_given_no_review_time_when_receipt_reviewed_published_then_time_is_none(sent):
    # Act
    events.publish_receipt_reviewed(1, 101, "REJECTED", None)

    # Assert
    assert sent[0][1]["reviewed_at"] is None
