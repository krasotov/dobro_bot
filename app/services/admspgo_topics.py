from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def fetch_published_topics() -> list[dict[str, Any]] | None:
    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        return None

    url = f"{base}/api/internal/max-bot/topics"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if response.status_code != 200:
                logger.warning("topics fetch %s: %s", response.status_code, response.text[:200])
                return None
            data = response.json()
            topics = data.get("topics") if isinstance(data, dict) else None
            if isinstance(topics, list):
                return [t for t in topics if isinstance(t, dict) and t.get("slug")]
    except Exception:
        logger.exception("Failed to fetch topics from admspgo")
    return None
