import threading
from contextlib import asynccontextmanager

import consumer
from fastapi import FastAPI
from router import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure database schema is created/up-to-date
    try:
        from database import Base, engine

        Base.metadata.create_all(bind=engine)
    except Exception as e:
        print(f"Schema creation error: {e}", flush=True)

    # Start RabbitMQ consumer in background thread on container startup
    consumer_thread = threading.Thread(target=consumer.start_consumer, daemon=True)
    consumer_thread.start()
    yield


app = FastAPI(title="Enrollment Service", lifespan=lifespan)

app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "enrollment_service"}
