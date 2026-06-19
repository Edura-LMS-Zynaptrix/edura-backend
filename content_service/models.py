from database import Base
from sqlalchemy import Column, Integer, String

class Content(Base):
    __tablename__ = "Content"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, index=True, nullable=False)
    title = Column(String, nullable=False)
    content_type = Column(String, nullable=False)
    url = Column(String, nullable=True)
    body = Column(String, nullable=True)
    order = Column(Integer, default=0)