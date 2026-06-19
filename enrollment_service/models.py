from database import Base
from sqlalchemy import Column, Integer

class Enrollment(Base):
    __tablename__ = "Enrollment"

    id = Column(Integer, primary_key=True, index=True)