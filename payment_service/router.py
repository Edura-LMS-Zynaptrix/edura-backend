import uuid
from datetime import datetime, timedelta, timezone

from content_client import upload_receipt
from course_client import get_course_price
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
from models import (
    BankAccount,
    Payment,
    PaymentMethod,
    PaymentReceipt,
    PaymentStatus,
    ReceiptStatus,
)
from schemas import (
    BankAccountCreate,
    BankAccountResponse,
    BankAccountUpdate,
    CheckoutRequest,
    CheckoutResponse,
    FreeEnrollRequest,
    FreeEnrollResponse,
    PaymentResponse,
    ReceiptAdminResponse,
    ReceiptUploadResponse,
    ReceiptVerifyRequest,
)
from sqlalchemy import func
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
# POST /free-enroll — Instant enrollment for price=0 courses
# ---------------------------------------------------------------------------
@router.post(
    "/free-enroll", response_model=FreeEnrollResponse, status_code=status.HTTP_200_OK
)
async def free_enroll(
    body: FreeEnrollRequest,
    payload: dict = Depends(require_role(["student"])),
    db: Session = Depends(get_db),
):
    """
    There is no direct "create an enrollment" endpoint anywhere — every
    enrollment is created by enrollment_service's consumer reacting to a
    payment.success event (from the PayHere webhook, or a manually-approved
    receipt). A price=0 course still needs the same event, just without an
    actual payment — this creates a $0 Payment record for the audit trail
    and publishes the identical event so enrollment_service treats it
    exactly like any other successful payment.
    """
    student_id = int(payload["sub"])

    price = await get_course_price(body.course_id)
    if price is None:
        raise HTTPException(status_code=404, detail={"error": "COURSE_NOT_FOUND"})
    if price != 0:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "COURSE_NOT_FREE",
                "message": "This course is not free — use checkout instead.",
            },
        )

    existing = (
        db.query(Payment)
        .filter(
            Payment.student_id == student_id,
            Payment.course_id == body.course_id,
            Payment.payment_status == PaymentStatus.SUCCESS,
        )
        .first()
    )
    if existing:
        return FreeEnrollResponse(
            message="Already enrolled", course_id=body.course_id, payment_id=existing.id
        )

    payment = Payment(
        student_id=student_id,
        course_id=body.course_id,
        payment_method=PaymentMethod.MANUAL,
        payment_status=PaymentStatus.SUCCESS,
        amount=0,
        currency="LKR",
        paid_at=datetime.now(timezone.utc),
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    publish_payment_success(
        student_id=student_id,
        course_id=body.course_id,
        order_id=f"FREE-{payment.id}",
        amount=0,
    )

    return FreeEnrollResponse(
        message="Enrolled successfully", course_id=body.course_id, payment_id=payment.id
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

    # Uploaded to Cloudinary via content_service, which owns the integration
    receipt_filename = (
        f"{student_id}_{course_id}_{uuid.uuid4().hex[:8]}_{file.filename}"
    )
    receipt_url, receipt_public_id = await upload_receipt(content, receipt_filename)

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
        receipt_public_id=receipt_public_id,
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
        receipt_public_id=receipt_public_id,
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
    role = payload.get("role", "").lower()
    if role == "admin":
        payments = db.query(Payment).all()
    else:
        payments = db.query(Payment).filter(Payment.student_id == student_id).all()
    return payments


# ---------------------------------------------------------------------------
# Admin: review manual bank-slip receipts
# ---------------------------------------------------------------------------
@router.get("/receipts", response_model=list[ReceiptAdminResponse])
async def list_receipts(
    status_filter: str | None = None,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    q = db.query(PaymentReceipt, Payment).join(
        Payment, PaymentReceipt.payment_id == Payment.id
    )
    if status_filter:
        try:
            q = q.filter(
                PaymentReceipt.receipt_status == ReceiptStatus(status_filter.upper())
            )
        except ValueError:
            raise HTTPException(status_code=422, detail={"error": "INVALID_STATUS"})
    rows = q.order_by(PaymentReceipt.created_at.desc()).all()
    return [
        ReceiptAdminResponse(
            id=receipt.id,
            payment_id=receipt.payment_id,
            student_id=receipt.student_id,
            course_id=payment.course_id,
            amount=float(payment.amount),
            receipt_url=receipt.receipt_url,
            receipt_public_id=receipt.receipt_public_id,
            status=receipt.receipt_status.value.lower(),
            reviewer_id=receipt.reviewer_id,
            reviewer_note=receipt.reviewer_note,
            reviewed_at=receipt.reviewed_at,
            created_at=receipt.created_at,
        )
        for receipt, payment in rows
    ]


@router.put("/receipts/{receipt_id}/verify", response_model=ReceiptAdminResponse)
async def verify_receipt(
    receipt_id: int,
    body: ReceiptVerifyRequest,
    payload: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    receipt = db.query(PaymentReceipt).filter(PaymentReceipt.id == receipt_id).first()
    if not receipt:
        raise HTTPException(status_code=404, detail={"error": "RECEIPT_NOT_FOUND"})
    if receipt.receipt_status != ReceiptStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "ALREADY_REVIEWED",
                "message": "This receipt has already been reviewed",
            },
        )

    payment = db.query(Payment).filter(Payment.id == receipt.payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail={"error": "PAYMENT_NOT_FOUND"})

    reviewer_id = int(payload["sub"])
    receipt.reviewer_id = reviewer_id
    receipt.reviewed_at = datetime.now(timezone.utc)

    if body.status == "verified":
        receipt.receipt_status = ReceiptStatus.APPROVED
        payment.payment_status = PaymentStatus.SUCCESS
        payment.paid_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(receipt)
        db.refresh(payment)

        # Same event the PayHere webhook publishes — enrollment_service's
        # consumer activates/extends the enrollment identically either way.
        publish_payment_success(
            student_id=payment.student_id,
            course_id=payment.course_id,
            order_id=f"MANUAL-{payment.id}",
            amount=float(payment.amount),
        )
    else:
        receipt.receipt_status = ReceiptStatus.REJECTED
        receipt.reviewer_note = body.rejection_reason
        payment.payment_status = PaymentStatus.FAILED
        db.commit()
        db.refresh(receipt)
        db.refresh(payment)

    return ReceiptAdminResponse(
        id=receipt.id,
        payment_id=receipt.payment_id,
        student_id=receipt.student_id,
        course_id=payment.course_id,
        amount=float(payment.amount),
        receipt_url=receipt.receipt_url,
        receipt_public_id=receipt.receipt_public_id,
        status=receipt.receipt_status.value.lower(),
        reviewer_id=receipt.reviewer_id,
        reviewer_note=receipt.reviewer_note,
        reviewed_at=receipt.reviewed_at,
        created_at=receipt.created_at,
    )


# ---------------------------------------------------------------------------
# Bank accounts — shown to students on the bank-slip page, managed by admins
# ---------------------------------------------------------------------------


def _get_bank_account_or_404(account_id: int, db: Session) -> BankAccount:
    account = db.query(BankAccount).filter(BankAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail={"error": "BANK_ACCOUNT_NOT_FOUND"})
    return account


SL_TIME = timezone(timedelta(hours=5, minutes=30))


def _sl(moment: datetime) -> datetime:
    return (
        moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    ).astimezone(SL_TIME)


@router.get("/internal/admin-stats")
def admin_stats_internal(db: Session = Depends(get_db)):
    """Service-to-service only (blocked at the gateway): revenue figures for the admin dashboard."""
    # A SUCCESS payment isn't edited after it succeeds, so updated_at is when it completed.
    # Free enrollments are recorded as 0.00 payments and aren't revenue.
    paid = (
        db.query(Payment.amount, Payment.updated_at)
        .filter(Payment.payment_status == PaymentStatus.SUCCESS, Payment.amount > 0)
        .all()
    )
    months: dict[tuple[int, int], list] = {}
    for amount, when in paid:
        local = _sl(when)
        bucket = months.setdefault((local.year, local.month), [0.0, 0])
        bucket[0] += float(amount)
        bucket[1] += 1
    now = datetime.now(SL_TIME)
    recent = (
        db.query(PaymentReceipt.student_id, PaymentReceipt.reviewed_at)
        .filter(
            PaymentReceipt.receipt_status == ReceiptStatus.APPROVED,
            PaymentReceipt.reviewed_at.isnot(None),
        )
        .order_by(PaymentReceipt.reviewed_at.desc())
        .limit(5)
        .all()
    )
    return {
        "total_revenue": round(sum(float(a) for a, _ in paid), 2),
        "revenue_this_month": round(months.get((now.year, now.month), [0.0])[0], 2),
        "pending_bank_slips": db.query(func.count(PaymentReceipt.id))
        .filter(PaymentReceipt.receipt_status == ReceiptStatus.PENDING)
        .scalar()
        or 0,
        "revenue_by_month": [
            {
                "year": y,
                "month": m,
                "revenue": round(months[(y, m)][0]),
                "count": months[(y, m)][1],
            }
            for (y, m) in sorted(months)[-12:]
        ],
        "recent_verified": [
            {"student_id": student_id, "verified_at": reviewed.isoformat()}
            for student_id, reviewed in recent
        ],
    }


@router.get("/bank-accounts", response_model=list[BankAccountResponse])
def list_active_bank_accounts(db: Session = Depends(get_db)):
    """Public: where to deposit before uploading a bank slip."""
    return (
        db.query(BankAccount)
        .filter(BankAccount.is_active.is_(True))
        .order_by(BankAccount.order_number, BankAccount.id)
        .all()
    )


@router.get("/admin/bank-accounts", response_model=list[BankAccountResponse])
def list_all_bank_accounts(
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    return (
        db.query(BankAccount).order_by(BankAccount.order_number, BankAccount.id).all()
    )


@router.post(
    "/admin/bank-accounts", response_model=BankAccountResponse, status_code=201
)
def create_bank_account(
    body: BankAccountCreate,
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    account = BankAccount(**body.model_dump())
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.patch("/admin/bank-accounts/{account_id}", response_model=BankAccountResponse)
def update_bank_account(
    account_id: int,
    body: BankAccountUpdate,
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    account = _get_bank_account_or_404(account_id, db)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    db.commit()
    db.refresh(account)
    return account


@router.delete("/admin/bank-accounts/{account_id}", status_code=204)
def delete_bank_account(
    account_id: int,
    _: dict = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    db.delete(_get_bank_account_or_404(account_id, db))
    db.commit()
