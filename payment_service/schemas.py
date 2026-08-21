from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CheckoutRequest(BaseModel):
    course_id: int
    amount: Optional[float] = Field(None, description="Course price in LKR")


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
