import logging
import os

import httpx

logger = logging.getLogger(__name__)

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user_service:8002")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth_service:8001")


async def display_names(user_ids: list[int]) -> dict[int, str]:
    """Profile names from user_service, falling back to the auth email; unknown ids are absent."""
    ids = sorted({i for i in user_ids if i is not None})
    if not ids:
        return {}
    names: dict[int, str] = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            r = await client.get(
                f"{USER_SERVICE_URL}/internal/names",
                params={"ids": ",".join(map(str, ids))},
            )
            if r.status_code == 200:
                names = {int(k): v for k, v in r.json().items() if v}
        except Exception as e:
            logger.warning("Name lookup failed: %s", e)
        for uid in ids:
            if uid in names:
                continue
            try:
                r = await client.get(f"{AUTH_SERVICE_URL}/internal/users/{uid}")
                if r.status_code == 200:
                    names[uid] = r.json().get("email") or ""
            except Exception:
                pass
    return {k: v for k, v in names.items() if v}
