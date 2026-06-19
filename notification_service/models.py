from database import Base
from sqlalchemy import Column, Integer

class Notification(Base):
    __tablename__ = "Notification"

    id = Column(Integer, primary_key=True, index=True)