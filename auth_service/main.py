import logging
import os
import sys

# Ensure auth_service and parent directory are in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Base, engine
from fastapi import FastAPI
from router import router
from sqlalchemy import text

logger = logging.getLogger(__name__)

app = FastAPI(title="Edura Auth Service")

app.include_router(router)

# create_all() only creates missing tables, never alters an existing one —
# these are the lightweight substitute for a real migration this project
# uses everywhere else (no Alembic run at runtime).
_COLUMN_MIGRATIONS = (
    "ALTER TABLE users ALTER COLUMN hashed_password DROP NOT NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider VARCHAR(20) NOT NULL DEFAULT 'email'",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_id ON users (google_id)",
)


@app.on_event("startup")
def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Auth service database tables verified.")
    except Exception as e:
        logger.error(f"Error initializing DB tables on startup: {e}")

    try:
        with engine.begin() as conn:
            for stmt in _COLUMN_MIGRATIONS:
                conn.execute(text(stmt))
        logger.info("Auth service column migrations applied.")
    except Exception as e:
        logger.error(f"Error applying column migrations on startup: {e}")


@app.get("/health")
def health():
    return {"status": "ok", "service": "auth_service"}
