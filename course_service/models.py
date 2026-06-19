from database import Base
from sqlalchemy import Column, Integer, String, Float, Boolean

class Course(Base):
    __tablename__ = "Course"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True, nullable=False)
    description = Column(String, nullable=True)
    instructor_id = Column(Integer, index=True, nullable=False)
    price = Column(Float, default=0.0)
    is_published = Column(Boolean, default=False)