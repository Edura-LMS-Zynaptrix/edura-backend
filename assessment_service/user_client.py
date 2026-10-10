import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user_service:8002")


async def get_profiles(user_ids: list[int]) -> Optional[dict[int, dict]]:
    """{user_id: profile} from user_service; None when it can't be reached (distinct from "none found")."""
    if not user_ids:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{USER_SERVICE_URL}/internal/profiles",
                params={"ids": ",".join(str(i) for i in sorted(set(user_ids)))},
            )
        r.raise_for_status()
        return {int(p["user_id"]): p for p in r.json()}
    except Exception as e:
        logger.error("Profile lookup failed: %r", e)
        return None
