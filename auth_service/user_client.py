import logging
import os

import httpx

logger = logging.getLogger(__name__)

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user_service:8002")


async def create_registration_profile(
    user_id: int, role: str, profile: dict
) -> tuple[bool, int, dict | str]:
    """
    Creates the profile for a brand-new email sign-up. Unlike the best-effort
    helper below, the caller needs to know if it worked: a sign-up without its
    profile is rolled back. Returns (ok, http_status, detail).
    """
    url = f"{USER_SERVICE_URL}/internal/create-profile"
    r = None
    # The database behind user_service can be slow to answer after sitting idle.
    # Creating a profile is idempotent (an existing one is returned), so a
    # second attempt after a timeout is safe.
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    url, json={"user_id": user_id, "role": role, **profile}
                )
            break
        except Exception as e:
            logger.error(
                f"Could not reach user_service to create profile for {user_id} (attempt {attempt}): {e!r}"
            )
    if r is None:
        return False, 503, "user_service unavailable"
    if r.status_code >= 400:
        logger.error(
            f"user_service rejected registration profile for {user_id}: {r.text}"
        )
        try:
            return False, r.status_code, r.json().get("detail", r.text)
        except Exception:
            return False, r.status_code, r.text
    return True, r.status_code, {}


async def delete_profile_internal(user_id: int) -> None:
    """Best-effort cleanup when a sign-up is rolled back (a profile may exist even if the reply was lost)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.delete(f"{USER_SERVICE_URL}/internal/profiles/{user_id}")
    except Exception as e:
        logger.error(f"Could not remove profile for rolled-back user {user_id}: {e!r}")


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
            logger.error(
                f"user_service rejected internal profile creation for {user_id}: {r.text}"
            )
    except Exception as e:
        logger.error(
            f"Could not reach user_service to create profile for {user_id}: {e}"
        )
