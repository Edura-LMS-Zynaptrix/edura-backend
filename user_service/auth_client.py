import logging
import os

import httpx

logger = logging.getLogger(__name__)

AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth_service:8001")


async def get_auth_user(user_id: int) -> dict | None:
    """Singular counterpart to get_all_auth_users() — used for single-record
    lookups (e.g. GET /{user_id}) instead of pulling the whole table."""
    url = f"{AUTH_SERVICE_URL}/internal/users/{user_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logger.error(f"Could not reach auth_service for /internal/users/{user_id}: {e}")
        return None


async def get_all_auth_users() -> dict[int, dict]:
    """
    Bulk-fetches every auth_service User row, keyed by id, so an admin-facing
    profile listing here can be enriched with is_active/email — fields that
    only exist on auth_service's User, not on a UserProfile. Best-effort:
    on failure, callers fall back to treating every user as active with no
    email, same degradation pattern used elsewhere in this project when an
    internal service call fails.
    """
    url = f"{AUTH_SERVICE_URL}/internal/users"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            logger.error(f"auth_service /internal/users returned {r.status_code}")
            return {}
        return {row["id"]: row for row in r.json()}
    except Exception as e:
        logger.error(f"Could not reach auth_service for /internal/users: {e}")
        return {}
