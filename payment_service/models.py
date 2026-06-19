from database import Base
from sqlalchemy import Column, Integer

class Payment(Base):
    __tablename__ = "Payment"

    id = Column(Integer, primary_key=True, index=True)