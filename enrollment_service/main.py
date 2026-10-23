from contextlib import asynccontextmanager

import consumer
from access_events import announce_everything
from database import SessionLocal
from fastapi import FastAPI
from router import router

from shared import messaging


def _resync() -> None:
    db = SessionLocal()
    try:
        announce_everything(db)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure database schema is created/up-to-date
    try:
        from database import Base, engine

        Base.metadata.create_all(bind=engine)
    except Exception as e:
        print(f"Schema creation error: {e}", flush=True)

    try:
        from database import engine
        from sqlalchemy import text

        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS is_manual BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
    except Exception as e:
        print(f"Column migration error: {e}", flush=True)

    # Listen for payments, course and user changes; tell the other services who can open what.
    consumer.start_consumer()
    messaging.resync_later(_resync)
    yield


app = FastAPI(title="Enrollment Service", lifespan=lifespan)

app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "enrollment_service"}
