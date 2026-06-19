from database import Base
from sqlalchemy import Column, Integer, Boolean, DateTime

class Progress(Base):
    __tablename__ = "Progress"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    course_id = Column(Integer, index=True, nullable=False)
    content_id = Column(Integer, index=True, nullable=False)
    is_completed = Column(Boolean, default=False)
    completed_at = Column(DateTime, nullable=True)