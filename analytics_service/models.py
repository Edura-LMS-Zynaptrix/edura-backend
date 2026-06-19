from database import Base
from sqlalchemy import Column, Integer

class Analytics(Base):
    __tablename__ = "Analytics"

    id = Column(Integer, primary_key=True, index=True)