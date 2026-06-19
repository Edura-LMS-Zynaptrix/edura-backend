from fastapi import FastAPI
from router import router

app = FastAPI(
    title="Content Service"
)

app.include_router(router)

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "content_service"
    }