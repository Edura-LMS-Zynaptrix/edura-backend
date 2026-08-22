import uuid
from datetime import datetime, timezone

from database import get_db
from events import publish_payment_success
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from models import Payment, PaymentMethod, PaymentReceipt, PaymentStatus, ReceiptStatus
from schemas import (
    CheckoutRequest,
    CheckoutResponse,
    PaymentResponse,
    ReceiptUploadResponse,
)
from sqlalchemy.orm import Session
from utils import (
    PAYHERE_CHECKOUT_URL,
    PAYHERE_MERCHANT_ID,
    PAYHERE_MERCHANT_SECRET,
    calculate_checkout_hash,
    verify_payhere_signature,
)

from shared.auth import require_role

router = APIRouter()

_STUDENT_OR_ADMIN = ["student", "admin"]
_ALL_ROLES = ["student", "teacher", "admin"]
ALLOWED_RECEIPT_MIME_TYPES = {"image/jpeg", "image/png", "application/pdf"}
MAX_RECEIPT_SIZE = 2 * 1024 * 1024  # 2 MB limit for receipts


# ---------------------------------------------------------------------------
# POST /checkout — Automated PayHere Checkout Init (AC1)
# ---------------------------------------------------------------------------
@router.post(
    "/checkout", response_model=CheckoutResponse, status_code=status.HTTP_200_OK
)
async def init_checkout(
    body: CheckoutRequest,
    payload: dict = Depends(require_role(_STUDENT_OR_ADMIN)),
    db: Session = Depends(get_db),
):
    student_id = int(payload["sub"])
    order_id = f"ORD-{uuid.uuid4().hex[:12].upper()}"
    amount = body.amount if body.amount is not None else 1000.00
    formatted_amount = f"{amount:.2f}"
    currency = "LKR"

    # Create pending payment record
    payment = Payment(
        student_id=student_id,
        course_id=body.course_id,
        payment_method=PaymentMethod.PAYHERE,
        payment_status=PaymentStatus.PENDING,
        amount=amount,
        currency=currency,
        payhere_order_id=order_id,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    # Calculate PayHere checkout hash
    checkout_hash = calculate_checkout_hash(
        merchant_id=PAYHERE_MERCHANT_ID,
        order_id=order_id,
        amount=formatted_amount,
        currency=currency,
        merchant_secret=PAYHERE_MERCHANT_SECRET,
    )

    return CheckoutResponse(
        order_id=order_id,
        merchant_id=PAYHERE_MERCHANT_ID,
        amount=amount,
        currency=currency,
        hash=checkout_hash,
        redirect_url=PAYHERE_CHECKOUT_URL,
    )


# ---------------------------------------------------------------------------
# POST /webhook — PayHere Server-to-Server Webhook Handler (AC2, AC3, AC5)
# ---------------------------------------------------------------------------
@router.post("/webhook", status_code=status.HTTP_200_OK)
async def payhere_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    form_data = await request.form()
    merchant_id = str(form_data.get("merchant_id", ""))
    order_id = str(form_data.get("order_id", ""))
    payhere_amount = str(form_data.get("payhere_amount", ""))
    payhere_currency = str(form_data.get("payhere_currency", ""))
    status_code = str(form_data.get("status_code", ""))
    received_md5sig = str(form_data.get("md5sig", ""))
    payhere_payment_id = str(form_data.get("payment_id", ""))

    # Verify HMAC-MD5 Signature (AC3)
    is_valid_sig = verify_payhere_signature(
        merchant_id=merchant_id,
        order_id=order_id,
        payhere_amount=payhere_amount,
        payhere_currency=payhere_currency,
        status_code=status_code,
        received_md5sig=received_md5sig,
    )

    if not is_valid_sig:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "INVALID_SIGNATURE",
                "message": "PayHere HMAC signature verification failed",
            },
        )

    # Lookup Payment record by order_id
    payment = db.query(Payment).filter(Payment.payhere_order_id == order_id).first()
    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "PAYMENT_NOT_FOUND",
                "message": f"No payment found for order {order_id}",
            },
        )

    # AC5: Idempotency check — if already processed as SUCCESS, return 200 silently
    if payment.payment_status == PaymentStatus.SUCCESS:
        return {"status": "SUCCESS", "message": "Webhook already processed"}

    # Update Payment status if status_code == 2 (PayHere Success)
    if status_code == "2":
        payment.payment_status = PaymentStatus.SUCCESS
        payment.paid_at = datetime.now(timezone.utc)
        payment.reference_no = payhere_payment_id
        db.commit()
        db.refresh(payment)

        # Publish payment.success event to RabbitMQ
        publish_payment_success(
            student_id=payment.student_id,
            course_id=payment.course_id,
            order_id=order_id,
            amount=float(payment.amount),
        )
        return {"status": "SUCCESS", "message": "Payment confirmed and event published"}
    else:
        payment.payment_status = PaymentStatus.FAILED
        db.commit()
        return {
            "status": "FAILED",
            "message": f"Payment failed with status code {status_code}",
        }


# ---------------------------------------------------------------------------
# POST /receipts — Manual Receipt Upload (AC4)
# ---------------------------------------------------------------------------
@router.post(
    "/receipts",
    response_model=ReceiptUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_manual_receipt(
    course_id: int = Form(...),
    amount: float = Form(...),
    file: UploadFile = File(...),
    payload: dict = Depends(require_role(_STUDENT_OR_ADMIN)),
    db: Session = Depends(get_db),
):
    student_id = int(payload["sub"])
    content = await file.read()

    # File size validation (<= 2MB)
    if len(content) > MAX_RECEIPT_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "FILE_TOO_LARGE",
                "message": "Receipt file must be <= 2MB",
            },
        )

    # MIME validation
    mime_type = file.content_type or ""
    if mime_type not in ALLOWED_RECEIPT_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "INVALID_MIME_TYPE",
                "message": f"MIME type '{mime_type}' is not allowed. Allowed: JPG, PNG, PDF",
            },
        )

    # Simulated Azure Blob URL / stored asset path
    receipt_filename = (
        f"receipts/{student_id}_{course_id}_{uuid.uuid4().hex[:8]}_{file.filename}"
    )
    receipt_url = f"https://edurastorage.blob.core.windows.net/{receipt_filename}"

    # Create Payment record with MANUAL method and PENDING status
    payment = Payment(
        student_id=student_id,
        course_id=course_id,
        payment_method=PaymentMethod.MANUAL,
        payment_status=PaymentStatus.PENDING,
        amount=amount,
        currency="LKR",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    # Create PaymentReceipt record
    receipt = PaymentReceipt(
        payment_id=payment.id,
        student_id=student_id,
        receipt_url=receipt_url,
        receipt_status=ReceiptStatus.PENDING,
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)

    return ReceiptUploadResponse(
        payment_id=payment.id,
        receipt_id=receipt.id,
        status="PENDING",
        receipt_url=receipt_url,
        message="Manual receipt uploaded successfully and is pending review",
    )


# ---------------------------------------------------------------------------
# GET / — List Student Payments
# ---------------------------------------------------------------------------
@router.get("/", response_model=list[PaymentResponse])
async def list_user_payments(
    payload: dict = Depends(require_role(_ALL_ROLES)),
    db: Session = Depends(get_db),
):
    student_id = int(payload["sub"])
    role = payload.get("role", "")
    if role == "admin":
        payments = db.query(Payment).all()
    else:
        payments = db.query(Payment).filter(Payment.student_id == student_id).all()
    return payments
