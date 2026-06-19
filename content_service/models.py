from database import Base
from sqlalchemy import Column, Integer

class Content(Base):
    __tablename__ = "Content"

    id = Column(Integer, primary_key=True, index=True)