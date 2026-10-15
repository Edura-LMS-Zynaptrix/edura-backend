import logging

from badges import seed_badges
from database import Base, SessionLocal, engine
from fastapi import FastAPI
from router import router
from sqlalchemy import text

logger = logging.getLogger(__name__)

app = FastAPI(title="Assessment Service")

# create_all() never adds columns to an existing table — same lightweight
# ADD COLUMN IF NOT EXISTS approach as the other services.
_COLUMN_MIGRATIONS = (
    "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS module_id INTEGER",
    "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS image_url VARCHAR(512)",
    "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS image_public_id VARCHAR(255)",
    "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS position INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS scheduled_start TIMESTAMP WITH TIME ZONE",
    "CREATE INDEX IF NOT EXISTS ix_assessments_module_id ON assessments (module_id)",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS explanation TEXT",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS question_image_url VARCHAR(512)",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS question_image_public_id VARCHAR(255)",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS option_images_json TEXT",
)


@app.on_event("startup")
def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Assessment service database tables verified.")
    except Exception as e:
        logger.error(f"Error initializing DB tables on startup: {e}")

    try:
        with engine.begin() as conn:
            for stmt in _COLUMN_MIGRATIONS:
                conn.execute(text(stmt))
        logger.info("Assessment service column migrations applied.")
    except Exception as e:
        logger.error(f"Error applying column migrations on startup: {e}")

    try:
        db = SessionLocal()
        try:
            seed_badges(db)
        finally:
            db.close()
        logger.info("Badges seeded.")
    except Exception as e:
        logger.error(f"Error seeding badges on startup: {e}")


# Registered before the router: /{assessment_id} is a plain wildcard path
# segment at the routing level, so it matches "health" too — whichever route
# is registered first wins that match, and its Depends() (auth) runs before
# FastAPI's own path-type validation ever gets a chance to 422 on "health".
@app.get("/health")
def health():
    return {"status": "ok", "service": "assessment_service"}


app.include_router(router)
