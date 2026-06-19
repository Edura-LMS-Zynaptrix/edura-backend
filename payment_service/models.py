import datetime
from database import Base
from sqlalchemy import Column, Integer, String, Float, DateTime

class Payment(Base):
    __tablename__ = "Payment"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    course_id = Column(Integer, index=True, nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="USD")
    status = Column(String, default="pending")
    transaction_id = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)