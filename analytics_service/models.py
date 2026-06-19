import datetime
from database import Base
from sqlalchemy import Column, Integer, String, DateTime

class Analytics(Base):
    __tablename__ = "Analytics"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=True)
    action = Column(String, nullable=False)
    details = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)