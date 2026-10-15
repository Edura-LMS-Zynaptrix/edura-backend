import logging
import os
import sys

# Ensure auth_service and parent directory are in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Base, engine
from fastapi import FastAPI
from router import router

logger = logging.getLogger(__name__)

app = FastAPI(title="Edura Auth Service")

app.include_router(router)


@app.on_event("startup")
def on_startup():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Auth service database tables verified.")
    except Exception as e:
        logger.error(f"Error initializing DB tables on startup: {e}")


@app.get("/health")
def health():
    return {"status": "ok", "service": "auth_service"}
