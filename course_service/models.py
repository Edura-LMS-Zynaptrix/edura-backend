from database import Base
from sqlalchemy import Column, Integer

class Course(Base):
    __tablename__ = "Course"

    id = Column(Integer, primary_key=True, index=True)