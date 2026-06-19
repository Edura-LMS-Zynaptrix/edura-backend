from pydantic import BaseModel

class PaymentBase(BaseModel):
    pass

class PaymentResponse(PaymentBase):
    id: int

    model_config = {
        "from_attributes": True
    }