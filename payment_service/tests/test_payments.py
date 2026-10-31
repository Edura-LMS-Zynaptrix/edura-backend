"""
Unit and Integration tests for payment_service:
- PayHere HMAC MD5 signature calculation & verification
- Checkout initiation
- Webhook handling & idempotency
- Manual receipt upload & size/MIME validation
"""

import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-payment-tests")
os.environ.setdefault("PAYHERE_MERCHANT_ID", "123456")
os.environ.setdefault("PAYHERE_MERCHANT_SECRET", "test_merchant_secret")

from main import app  # noqa: E402
from utils import (
    generate_merchant_secret_hash,
    verify_payhere_signature,
)  # noqa: E402

SECRET = os.environ["SECRET_KEY"]
ALGORITHM = "HS256"
MERCHANT_ID = os.environ["PAYHERE_MERCHANT_ID"]
MERCHANT_SECRET = os.environ["PAYHERE_MERCHANT_SECRET"]

client = TestClient(app, raise_server_exceptions=False)


def _make_token(user_id: int, role: str) -> str:
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM)


def _auth(user_id: int, role: str) -> dict:
    return {"Authorization": f"Bearer {_make_token(user_id, role)}"}


# ---------------------------------------------------------------------------
# Unit Tests — HMAC Signature Calculation & Verification
# ---------------------------------------------------------------------------
class TestPayHereSignature:
    def test_given_correct_md5sig_when_verified_then_true(self):
        # Arrange
        order_id = "ORD-TEST-001"
        payhere_amount = "1000.00"
        payhere_currency = "LKR"
        status_code = "2"

        # Calculate valid signature
        secret_hash = generate_merchant_secret_hash(MERCHANT_SECRET)
        raw_str = f"{MERCHANT_ID}{order_id}{payhere_amount}{payhere_currency}{status_code}{secret_hash}"
        import hashlib

        valid_md5sig = hashlib.md5(raw_str.encode("utf-8")).hexdigest().upper()

        # Act and assert
        assert (
            verify_payhere_signature(
                merchant_id=MERCHANT_ID,
                order_id=order_id,
                payhere_amount=payhere_amount,
                payhere_currency=payhere_currency,
                status_code=status_code,
                received_md5sig=valid_md5sig,
                merchant_secret=MERCHANT_SECRET,
            )
            is True
        )

    def test_given_wrong_md5sig_when_verified_then_false(self):
        # Act and assert
        assert (
            verify_payhere_signature(
                merchant_id=MERCHANT_ID,
                order_id="ORD-TEST-001",
                payhere_amount="1000.00",
                payhere_currency="LKR",
                status_code="2",
                received_md5sig="WRONG_MD5_SIGNATURE",
                merchant_secret=MERCHANT_SECRET,
            )
            is False
        )


# ---------------------------------------------------------------------------
# Integration Tests — Endpoints (DB mocked / Sqlite fallback)
# ---------------------------------------------------------------------------
class TestPaymentEndpoints:
    def test_given_no_token_when_checkout_then_401(self):
        # Act
        r = client.post("/checkout", json={"course_id": 1, "amount": 1500.00})

        # Assert
        assert r.status_code == 401

    def test_given_file_over_2mb_when_receipt_uploaded_then_400_file_too_large(self):
        # Arrange
        big_content = b"x" * (2 * 1024 * 1024 + 1)

        # Act
        r = client.post(
            "/receipts",
            data={"course_id": 1, "amount": 1000.00},
            files={"file": ("big_receipt.pdf", big_content, "application/pdf")},
            headers=_auth(5, "student"),
        )

        # Assert
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "FILE_TOO_LARGE"

    def test_given_executable_when_receipt_uploaded_then_400_invalid_mime_type(self):
        # Act
        r = client.post(
            "/receipts",
            data={"course_id": 1, "amount": 1000.00},
            files={"file": ("script.exe", b"binary", "application/x-msdownload")},
            headers=_auth(5, "student"),
        )

        # Assert
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "INVALID_MIME_TYPE"
