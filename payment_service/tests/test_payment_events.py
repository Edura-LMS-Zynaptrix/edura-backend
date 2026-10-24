"""payment_service without any other service: prices come from its own copy of the catalogue,
receipts go straight to storage, and everything that matters is announced as an event.
"""

import os
from datetime import datetime, timedelta, timezone

# Same settings as test_payments.py, set before the app (and shared.auth) is imported.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-payment-tests")
os.environ.setdefault("PAYHERE_MERCHANT_ID", "123456")
os.environ.setdefault("PAYHERE_MERCHANT_SECRET", "test_merchant_secret")

import event_consumer  # noqa: E402
import events
import jwt
import pytest
from database import Base, get_db
from fastapi.testclient import TestClient
from main import app
from models import Payment, PaymentReceipt, PaymentStatus, ReplicaCourse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

SECRET = os.environ["SECRET_KEY"]
PDF = b"%PDF-1.4\n%receipt\n"


def _auth(user_id: int, role: str = "student") -> dict:
    token = jwt.encode(
        {
            "sub": str(user_id),
            "role": role,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)

    def override():
        db = session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    monkeypatch.setattr(event_consumer, "SessionLocal", session)

    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        events, "publish", lambda key, payload: sent.append((key, payload)) or True
    )
    monkeypatch.setattr(
        "shared.media.upload",
        lambda content, filename, folder, resource_type="auto": (
            f"{folder}/stored",
            "https://res.cloudinary.com/demo/receipt.pdf",
        ),
    )
    yield TestClient(app, raise_server_exceptions=False), session, sent
    app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=engine)


def _course(session, course_id, price):
    event_consumer.handle(
        "course.upserted",
        {
            "id": course_id,
            "title": "Maths",
            "price": price,
            "status": "PUBLISHED",
            "instructor_id": 2,
        },
    )


def test_the_course_price_copy_follows_catalogue_events(env):
    _, session, _ = env
    _course(session, 5, 1500)
    assert float(session().get(ReplicaCourse, 5).price) == 1500
    event_consumer.handle(
        "course.upserted",
        {"id": 5, "title": "Maths", "price": 900, "status": "PUBLISHED"},
    )
    assert float(session().get(ReplicaCourse, 5).price) == 900
    event_consumer.handle("course.deleted", {"id": 5})
    assert session().get(ReplicaCourse, 5) is None


def test_free_enrollment_is_decided_from_the_local_price(env):
    client, session, sent = env
    _course(session, 1, 0)
    _course(session, 2, 2500)

    free = client.post("/free-enroll", json={"course_id": 1}, headers=_auth(7))
    assert free.status_code == 200
    key, payload = sent[-1]
    assert key == "payment.success" and payload["order_id"].startswith("FREE-")
    assert (
        payload["student_id"] == 7
        and payload["course_id"] == 1
        and payload["amount"] == 0
    )
    assert payload["payment_id"] and payload["completed_at"]

    paid = client.post("/free-enroll", json={"course_id": 2}, headers=_auth(7))
    assert (
        paid.status_code == 400 and paid.json()["detail"]["error"] == "COURSE_NOT_FREE"
    )
    unknown = client.post("/free-enroll", json={"course_id": 99}, headers=_auth(7))
    assert (
        unknown.status_code == 404
        and unknown.json()["detail"]["error"] == "COURSE_NOT_FOUND"
    )


def test_receipt_is_checked_by_its_bytes_stored_and_announced(env):
    client, session, sent = env

    ok = client.post(
        "/receipts",
        data={"course_id": 3, "amount": 1500},
        files={"file": ("slip.pdf", PDF, "application/pdf")},
        headers=_auth(7),
    )
    assert ok.status_code == 201
    body = ok.json()
    assert body["receipt_url"] == "https://res.cloudinary.com/demo/receipt.pdf"
    assert body["receipt_public_id"] == "edura/receipts/stored"
    stored = session().query(PaymentReceipt).one()
    assert stored.receipt_public_id == "edura/receipts/stored"
    assert (
        "payment.receipt-uploaded",
        {"receipt_id": stored.id, "payment_id": stored.payment_id, "student_id": 7},
    ) in sent

    # a file that only claims to be a PDF is refused, and nothing is created for it
    fake = client.post(
        "/receipts",
        data={"course_id": 3, "amount": 1500},
        files={"file": ("slip.pdf", b"MZ not a pdf", "application/pdf")},
        headers=_auth(7),
    )
    assert (
        fake.status_code == 415
        and fake.json()["detail"]["error"] == "UNSUPPORTED_FILE_TYPE"
    )
    assert session().query(PaymentReceipt).count() == 1


def test_approving_a_receipt_announces_the_payment_and_the_review(env):
    client, session, sent = env
    client.post(
        "/receipts",
        data={"course_id": 3, "amount": 1500},
        files={"file": ("slip.pdf", PDF, "application/pdf")},
        headers=_auth(7),
    )
    receipt = session().query(PaymentReceipt).one()
    sent.clear()

    r = client.put(
        f"/receipts/{receipt.id}/verify",
        json={"status": "verified"},
        headers=_auth(1, "admin"),
    )

    assert r.status_code == 200
    keys = [k for k, _ in sent]
    assert keys == ["payment.success", "payment.receipt-reviewed"]
    success = sent[0][1]
    assert (
        success["order_id"] == f"MANUAL-{receipt.payment_id}"
        and success["amount"] == 1500
    )
    assert sent[1][1]["status"] == "APPROVED" and sent[1][1]["student_id"] == 7
    assert (
        session().get(Payment, receipt.payment_id).payment_status
        == PaymentStatus.SUCCESS
    )


def test_rejecting_a_receipt_announces_only_the_review(env):
    client, session, sent = env
    client.post(
        "/receipts",
        data={"course_id": 3, "amount": 1500},
        files={"file": ("slip.pdf", PDF, "application/pdf")},
        headers=_auth(7),
    )
    receipt = session().query(PaymentReceipt).one()
    sent.clear()

    r = client.put(
        f"/receipts/{receipt.id}/verify",
        json={"status": "rejected", "rejection_reason": "blurry"},
        headers=_auth(1, "admin"),
    )

    assert r.status_code == 200
    assert [k for k, _ in sent] == ["payment.receipt-reviewed"]
    assert sent[0][1]["status"] == "REJECTED"
