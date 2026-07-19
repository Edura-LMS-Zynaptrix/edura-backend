"""
content_service/models.py
SQLAlchemy 2.0 ORM models for the Content Service.

Per the SDS issue specification, the content_service has NO owned database tables.
It operates on Cloudinary/YouTube URLs stored in course_service.lessons.
This file is intentionally minimal — only the Base import is provided
so Alembic does not produce an empty migration error.

If a future requirement adds content metadata (e.g. processing job tracking),
add models below.
"""

from database import (
    Base,  # noqa: F401 — imported so Alembic detects this service's Base
)

# No tables owned by content_service.
# All media URL references live in course_service.lessons (video_url).
