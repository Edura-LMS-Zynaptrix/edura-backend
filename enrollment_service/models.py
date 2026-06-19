import datetime
from database import Base
from sqlalchemy import Column, Integer, String, DateTime

class Enrollment(Base):
    __tablename__ = "Enrollment"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    course_id = Column(Integer, index=True, nullable=False)
    enrolled_at = Column(DateTime, default=datetime.datetime.utcnow)
    status = Column(String, default="active")