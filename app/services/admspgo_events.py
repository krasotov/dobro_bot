from __future__ import annotations

import html
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def fetch_open_events() -> list[dict[str, Any]] | None:
    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        return None

    url = f"{base}/api/internal/max-bot/events"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if response.status_code != 200:
                logger.warning("events fetch %s: %s", response.status_code, response.text[:200])
                return None
            data = response.json()
            events = data.get("events") if isinstance(data, dict) else None
            if isinstance(events, list):
                return [e for e in events if isinstance(e, dict) and e.get("id")]
    except Exception:
        logger.exception("Failed to fetch events from admspgo")
    return None


async def register_for_event(
    post_id: str,
    *,
    max_user_id: int,
    reporter_name: str,
    reporter_phone: str | None,
    reporter_district: str | None,
) -> tuple[bool, str]:
    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        return False, "Запись временно недоступна."

    url = f"{base}/api/internal/max-bot/events/{post_id}/register"
    payload = {
        "maxUserId": str(max_user_id),
        "reporterName": reporter_name,
        "reporterPhone": reporter_phone,
        "reporterDistrict": reporter_district,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            if response.status_code == 201:
                return True, "Вы записаны на мероприятие."
            try:
                err = response.json().get("error", "")
            except Exception:
                err = response.text[:200]
            return False, err or "Не удалось записаться."
    except Exception:
        logger.exception("Event registration failed")
        return False, "Не удалось записаться. Попробуйте позже."
