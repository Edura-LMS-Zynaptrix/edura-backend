import hashlib
import os

PAYHERE_MERCHANT_ID = os.getenv("PAYHERE_MERCHANT_ID", "123456")
PAYHERE_MERCHANT_SECRET = os.getenv("PAYHERE_MERCHANT_SECRET", "secret_key_dev_123")
PAYHERE_CHECKOUT_URL = os.getenv(
    "PAYHERE_CHECKOUT_URL", "https://sandbox.payhere.lk/pay/checkout"
)


def generate_merchant_secret_hash(secret: str) -> str:
    """Calculates upper-case MD5 hash of merchant secret."""
    return hashlib.md5(secret.encode("utf-8")).hexdigest().upper()


def calculate_checkout_hash(
    merchant_id: str, order_id: str, amount: str, currency: str, merchant_secret: str
) -> str:
    """
    Calculates PayHere checkout initialization hash:
    hash = MD5(merchant_id + order_id + amount + currency + MD5(merchant_secret).upper()).upper()
    """
    secret_hash = generate_merchant_secret_hash(merchant_secret)
    # Format amount string if needed e.g. "1000.00"
    raw_str = f"{merchant_id}{order_id}{amount}{currency}{secret_hash}"
    return hashlib.md5(raw_str.encode("utf-8")).hexdigest().upper()


def verify_payhere_signature(
    merchant_id: str,
    order_id: str,
    payhere_amount: str,
    payhere_currency: str,
    status_code: str,
    received_md5sig: str,
    merchant_secret: str = PAYHERE_MERCHANT_SECRET,
) -> bool:
    """
    Verifies PayHere webhook HMAC-MD5 signature:
    md5sig = MD5(merchant_id + order_id + payhere_amount + payhere_currency + status_code + MD5(merchant_secret).upper()).upper()
    """
    secret_hash = generate_merchant_secret_hash(merchant_secret)
    raw_str = f"{merchant_id}{order_id}{payhere_amount}{payhere_currency}{status_code}{secret_hash}"
    calculated_sig = hashlib.md5(raw_str.encode("utf-8")).hexdigest().upper()
    return calculated_sig == received_md5sig.upper()
