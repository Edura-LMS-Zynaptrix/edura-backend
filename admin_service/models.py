from database import Base
from sqlalchemy import Column, Integer, String

class Admin(Base):
    __tablename__ = "Admin"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True, nullable=False)
    value = Column(String, nullable=True)
    description = Column(String, nullable=True)