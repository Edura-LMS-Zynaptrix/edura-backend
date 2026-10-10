from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class CheckoutRequest(BaseModel):
    course_id: int
    amount: Optional[float] = Field(None, description="Course price in LKR")


class FreeEnrollRequest(BaseModel):
    course_id: int


class FreeEnrollResponse(BaseModel):
    message: str
    course_id: int
    payment_id: int


class CheckoutResponse(BaseModel):
    order_id: str
    merchant_id: str
    amount: float
    currency: str
    hash: str
    redirect_url: str


class ReceiptUploadResponse(BaseModel):
    payment_id: int
    receipt_id: int
    status: str
    receipt_url: str
    receipt_public_id: Optional[str] = None
    message: str


class PaymentResponse(BaseModel):
    id: int
    student_id: int
    course_id: int
    payment_method: str
    payment_status: str
    amount: float
    currency: str
    payhere_order_id: Optional[str] = None
    reference_no: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReceiptAdminResponse(BaseModel):
    id: int
    payment_id: int
    student_id: int
    course_id: int
    amount: float
    receipt_url: str
    receipt_public_id: Optional[str] = None
    status: str
    reviewer_id: Optional[int] = None
    reviewer_note: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime


class ReceiptVerifyRequest(BaseModel):
    status: Literal["verified", "rejected"]
    rejection_reason: Optional[str] = None


class BankAccountCreate(BaseModel):
    bank_name: str = Field(..., min_length=1, max_length=120)
    account_number: str = Field(..., min_length=1, max_length=60)
    account_name: str = Field(..., min_length=1, max_length=160)
    branch: str = Field(..., min_length=1, max_length=120)
    is_active: bool = True
    order_number: int = 0


class BankAccountUpdate(BaseModel):
    bank_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    account_number: Optional[str] = Field(default=None, min_length=1, max_length=60)
    account_name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    branch: Optional[str] = Field(default=None, min_length=1, max_length=120)
    is_active: Optional[bool] = None
    order_number: Optional[int] = None


class BankAccountResponse(BaseModel):
    id: int
    bank_name: str
    account_number: str
    account_name: str
    branch: str
    is_active: bool
    order_number: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

