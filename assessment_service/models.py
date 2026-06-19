from database import Base
from sqlalchemy import Column, Integer

class Assessment(Base):
    __tablename__ = "Assessment"

    id = Column(Integer, primary_key=True, index=True)