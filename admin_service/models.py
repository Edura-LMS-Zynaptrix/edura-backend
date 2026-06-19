from database import Base
from sqlalchemy import Column, Integer

class Admin(Base):
    __tablename__ = "Admin"

    id = Column(Integer, primary_key=True, index=True)