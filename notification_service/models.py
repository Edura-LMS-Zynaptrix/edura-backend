import datetime
from database import Base
from sqlalchemy import Column, Integer, String, Boolean, DateTime

class Notification(Base):
    __tablename__ = "Notification"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)