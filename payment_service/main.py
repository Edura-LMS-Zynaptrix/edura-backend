from fastapi import FastAPI
from router import router

app = FastAPI(title="Payment Service")

app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "payment_service"}
