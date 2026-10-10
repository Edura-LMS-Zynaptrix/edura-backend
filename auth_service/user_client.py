import logging
import os

import httpx

logger = logging.getLogger(__name__)

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user_service:8002")


async def create_profile_internal(
    user_id: int,
    role: str,
    first_name: str,
    last_name: str,
    avatar_url: str | None = None,
) -> None:
    """
    Creates the matching user_service profile row for an account auth_service
    made itself (an admin created by another admin, or a first Google
    sign-in). Best-effort: logged, not fatal — without a profile the user is
    simply routed to complete-profile, which creates it.
    """
    url = f"{USER_SERVICE_URL}/internal/create-profile"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                url,
                json={
                    "user_id": user_id,
                    "role": role,
                    "first_name": first_name,
                    "last_name": last_name,
                    "avatar_url": avatar_url,
                },
            )
        if r.status_code >= 400:
            logger.error(f"user_service rejected internal profile creation for {user_id}: {r.text}")
    except Exception as e:
        logger.error(f"Could not reach user_service to create profile for {user_id}: {e}")
