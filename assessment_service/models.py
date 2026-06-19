from database import Base
from sqlalchemy import Column, Integer, String

class Assessment(Base):
    __tablename__ = "Assessment"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, index=True, nullable=False)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    max_score = Column(Integer, default=100)