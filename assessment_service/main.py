import logging

from database import Base, engine
from fastapi import FastAPI
from router import router

logger = logging.getLogger(__name__)

app = FastAPI(title="Assessment Service")

app.include_router(router)


@app.on_event("startup")
def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Assessment service database tables verified.")
    except Exception as e:
        logger.error(f"Error initializing DB tables on startup: {e}")


@app.get("/health")
def health():
    return {"status": "ok", "service": "assessment_service"}
