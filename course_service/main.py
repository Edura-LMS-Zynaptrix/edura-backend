import logging

import event_consumer
from database import Base, SessionLocal, engine
from events import announce_everything
from fastapi import FastAPI
from router import router
from sqlalchemy import text

from shared import messaging

logger = logging.getLogger(__name__)

app = FastAPI(title="Course Service")

# create_all() only creates missing tables — it never adds columns to a
# table that already exists. These ADD COLUMN IF NOT EXISTS statements are
# this project's lightweight substitute for a real Alembic migration run
# (alembic is configured per service but nothing actually invokes it at
# container startup — create_all is the only schema step that runs).
_COLUMN_MIGRATIONS = (
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS thumbnail_public_id VARCHAR(255)",
    "ALTER TABLE lessons ADD COLUMN IF NOT EXISTS cloudinary_public_id VARCHAR(255)",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS grade INTEGER",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS stream_ids JSON",
    "ALTER TABLE modules ADD COLUMN IF NOT EXISTS image_url VARCHAR(512)",
    "ALTER TABLE modules ADD COLUMN IF NOT EXISTS image_public_id VARCHAR(255)",
    "ALTER TABLE modules ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE lessons ADD COLUMN IF NOT EXISTS description TEXT",
)


@app.on_event("startup")
def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Course service database tables verified.")
    except Exception as e:
        # Harmless on a database that already has these tables/indexes from
        # an earlier run — logged, not fatal. Kept in its own try/except so
        # it can never block the column migrations below.
        logger.error(f"Error initializing DB tables on startup: {e}")

    try:
        with engine.begin() as conn:
            for stmt in _COLUMN_MIGRATIONS:
                conn.execute(text(stmt))
        logger.info("Course service column migrations applied.")
    except Exception as e:
        logger.error(f"Error applying column migrations on startup: {e}")

    event_consumer.start()
    messaging.resync_later(_resync)


def _resync() -> None:
    db = SessionLocal()
    try:
        announce_everything(db)
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok", "service": "course_service"}


app.include_router(router, tags=["courses"])
