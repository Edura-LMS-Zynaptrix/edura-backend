from fastapi import FastAPI
from router import router
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Auth Service"
)

origins = ["http://localhost:8001"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "UPDATE", "DELETE"],
    allow_headers=["*"]
)

app.include_router(router)

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "auth_service"
    }