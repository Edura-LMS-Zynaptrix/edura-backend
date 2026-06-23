"""
analytics_service/models.py
SQLAlchemy 2.0 ORM models for the Analytics Service.

Per the SDS issue specification, the analytics_service reads from other services
via direct DB queries or RabbitMQ events — it does NOT own its own primary tables.
This file provides only the Base import for Alembic compatibility.

If a future requirement adds aggregated analytics snapshots (e.g. DailyActiveUsers,
CourseViewCount), add models here.
"""
from database import Base  # noqa: F401 — imported so Alembic detects this service's Base

# No owned tables in analytics_service.
# This service aggregates data from other services' databases via read replicas
# or event-driven projections consumed from RabbitMQ.