import logging
import threading
from contextlib import asynccontextmanager

import consumer
from database import Base, engine
from fastapi import FastAPI
from router import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure database schema is created/up-to-date
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Progress service database tables verified.")
    except Exception as e:
        logger.error(f"Error initializing DB tables on startup: {e}")

    # Start RabbitMQ progress consumer in background daemon thread
    consumer_thread = threading.Thread(target=consumer.start_consumer, daemon=True)
    consumer_thread.start()
    logger.info("Background RabbitMQ consumer thread started for Progress Service.")

    yield


app = FastAPI(title="Progress Service", lifespan=lifespan)

app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "progress_service"}
