import os
import sys

# Ensure auth_service and parent directory (edura-backend-2) are in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from router import router
from database import Base, engine

# Ensure DB tables exist on startup
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Edura Auth Service")

app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "auth_service"}
