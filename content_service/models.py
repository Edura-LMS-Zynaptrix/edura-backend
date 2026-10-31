"""
content_service/models.py
SQLAlchemy 2.0 ORM models for the Content Service.

Media URLs live with the data that uses them (course lessons, receipts, question
images). This service only keeps a copy of what each student may open, which
enrollment_service announces, so it can gate paid media without asking anyone.
"""

from database import Base

from shared.replicas import define_replica_access

# Written only by the event consumer.
ReplicaAccess = define_replica_access(Base)
