import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SECRET_KEY: str = os.getenv("SECRET_KEY", "edura_super_secret_jwt_key_2026_dev")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15  # 900 seconds
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_URL: str = os.getenv("REDIS_URL", f"redis://{REDIS_HOST}:{REDIS_PORT}/0")

    ASGARDEO_CLIENT_ID: str = os.getenv("ASGARDEO_CLIENT_ID", "")
    ASGARDEO_CLIENT_SECRET: str = os.getenv("ASGARDEO_CLIENT_SECRET", "")
    ASGARDEO_TENANT_URL: str = os.getenv("ASGARDEO_TENANT_URL", "")


settings = Settings()
