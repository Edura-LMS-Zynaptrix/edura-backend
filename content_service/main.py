import logging

import event_consumer
from database import Base, engine
from fastapi import FastAPI
from router import router

logger = logging.getLogger(__name__)

app = FastAPI(title="Content Service")


@app.on_event("startup")
def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Content service database tables verified.")
    except Exception as e:
        logger.error(f"Error initializing DB tables on startup: {e}")

    event_consumer.start()


@app.get("/health")
def health():
    return {"status": "ok", "service": "content_service"}


app.include_router(router)
