from database import Base
from sqlalchemy import Column, Integer

class Progress(Base):
    __tablename__ = "Progress"

    id = Column(Integer, primary_key=True, index=True)