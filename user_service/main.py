from fastapi import FastAPI
from router import router

app = FastAPI(title="User Service")


@app.get("/health")
def health():
    return {"status": "ok", "service": "user_service"}


app.include_router(router, tags=["users"])
