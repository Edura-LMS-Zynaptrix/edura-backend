import logging

from database import Base, engine
from fastapi import FastAPI
from chat_router import router as chat_router
from router import router

logger = logging.getLogger(__name__)

app = FastAPI(title="Admin Service")

app.include_router(router)
app.include_router(chat_router)


@app.on_event("startup")
def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Admin service database tables verified.")
    except Exception as e:
        logger.error(f"Error initializing DB tables on startup: {e}")


@app.get("/health")
def health():
    return {"status": "ok", "service": "admin_service"}
