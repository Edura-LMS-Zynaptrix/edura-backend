import logging

from database import Base, engine
from fastapi import FastAPI
from router import router
from sqlalchemy import text

logger = logging.getLogger(__name__)

app = FastAPI(title="Payment Service")

app.include_router(router)

# create_all() only creates missing tables, never adds columns to one that
# already exists — this ADD COLUMN IF NOT EXISTS is the lightweight
# substitute for a real migration this project uses everywhere else.
_COLUMN_MIGRATIONS = (
    "ALTER TABLE payment_receipts ADD COLUMN IF NOT EXISTS receipt_public_id VARCHAR(255)",
)


@app.on_event("startup")
def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Payment service database tables verified.")
    except Exception as e:
        # Harmless on a database that already has these tables/indexes from
        # an earlier run — logged, not fatal. Kept in its own try/except so
        # it can never block the column migrations below.
        logger.error(f"Error initializing DB tables on startup: {e}")

    try:
        with engine.begin() as conn:
            for stmt in _COLUMN_MIGRATIONS:
                conn.execute(text(stmt))
        logger.info("Payment service column migrations applied.")
    except Exception as e:
        logger.error(f"Error applying column migrations on startup: {e}")


@app.get("/health")
def health():
    return {"status": "ok", "service": "payment_service"}
