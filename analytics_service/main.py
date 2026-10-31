import logging

import event_consumer
from database import Base, engine
from fastapi import FastAPI
from router import router

logger = logging.getLogger(__name__)

app = FastAPI(title="Analytics Service")

app.include_router(router)


@app.on_event("startup")
def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Analytics service database tables verified.")
    except Exception as e:
        logger.error(f"Error initializing DB tables on startup: {e}")

    # Build every report from copies of other services' data, kept current by events.
    event_consumer.start()


@app.get("/health")
def health():
    return {"status": "ok", "service": "analytics_service"}
