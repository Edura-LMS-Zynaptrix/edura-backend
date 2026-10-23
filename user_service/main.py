import logging

import event_consumer
from database import Base, SessionLocal, engine
from fastapi import FastAPI
from models import GradeSubject, Stream
from router import router
from sqlalchemy import text

from shared import messaging

logger = logging.getLogger(__name__)

# O/L core subjects (grades 6-11) and A/L streams (grades 12-13) — seeded once
# so the registration form has real choices instead of an empty catalogue.
_OL_SUBJECTS = [
    "Sinhala",
    "English",
    "Mathematics",
    "Science",
    "History",
    "Religion",
    "ICT",
    "Geography",
    "Civic Education",
    "Health & Physical Education",
]
_AL_STREAMS = [
    ("Physical Science", ["Combined Mathematics", "Physics", "Chemistry"]),
    ("Biological Science", ["Biology", "Physics", "Chemistry"]),
    ("Commerce", ["Business Studies", "Accounting", "Economics"]),
    ("Arts", ["Political Science", "Geography", "Logic & Scientific Method"]),
    ("Technology", ["Engineering Technology", "Science for Technology", "ICT"]),
]


def _seed_reference_data():
    db = SessionLocal()
    try:
        if db.query(GradeSubject).count() == 0:
            for grade in range(6, 12):
                for name in _OL_SUBJECTS:
                    db.add(GradeSubject(name=name, grade=grade, is_active=True))
            db.commit()
            logger.info("Seeded default grade_subjects catalogue.")

        if db.query(Stream).count() == 0:
            for name, subjects in _AL_STREAMS:
                db.add(Stream(name=name, subjects=subjects, is_active=True))
            db.commit()
            logger.info("Seeded default streams catalogue.")
    except Exception as e:
        logger.error(f"Error seeding reference data: {e}")
        db.rollback()
    finally:
        db.close()


app = FastAPI(title="User Service")

# create_all() only creates missing tables, never adds columns to one that
# already exists — this ADD COLUMN IF NOT EXISTS is the lightweight
# substitute for a real migration this project uses everywhere else.
_COLUMN_MIGRATIONS = (
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS avatar_public_id VARCHAR(255)",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS school VARCHAR(200)",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS district VARCHAR(100)",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS grade INTEGER",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS stream_id INTEGER",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS nic_number VARCHAR(20)",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS selected_subjects JSON",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS referral_code VARCHAR(20)",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS referred_by_user_id INTEGER",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_user_profiles_referral_code ON user_profiles (referral_code)",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS wallet_credits INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS total_credits_earned INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS total_successful_referrals INTEGER NOT NULL DEFAULT 0",
)


@app.on_event("startup")
def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("User service database tables verified.")
    except Exception as e:
        # Harmless on a database that already has these tables/indexes from
        # an earlier run — logged, not fatal. Kept in its own try/except so
        # it can never block the column migrations below.
        logger.error(f"Error initializing DB tables on startup: {e}")

    try:
        with engine.begin() as conn:
            for stmt in _COLUMN_MIGRATIONS:
                conn.execute(text(stmt))
        logger.info("User service column migrations applied.")
    except Exception as e:
        logger.error(f"Error applying column migrations on startup: {e}")

    _seed_reference_data()

    # Listens for new accounts (to create their profile) and enrollments (referral credit).
    event_consumer.start()
    messaging.resync_later(event_consumer.resync)


@app.get("/health")
def health():
    return {"status": "ok", "service": "user_service"}


app.include_router(router, tags=["users"])
