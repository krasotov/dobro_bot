from __future__ import annotations

import html
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def fetch_open_polls() -> list[dict[str, Any]] | None:
    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        return None

    url = f"{base}/api/internal/max-bot/polls"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if response.status_code != 200:
                logger.warning("polls fetch %s: %s", response.status_code, response.text[:200])
                return None
            data = response.json()
            polls = data.get("polls") if isinstance(data, dict) else None
            if isinstance(polls, list):
                return [p for p in polls if isinstance(p, dict) and p.get("id")]
    except Exception:
        logger.exception("Failed to fetch polls from admspgo")
    return None


async def fetch_poll_by_id(poll_id: str) -> dict[str, Any] | None:
    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        return None

    url = f"{base}/api/internal/max-bot/polls/{poll_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if response.status_code != 200:
                return None
            data = response.json()
            poll = data.get("poll") if isinstance(data, dict) else None
            if isinstance(poll, dict) and poll.get("id"):
                return poll
    except Exception:
        logger.exception("Failed to fetch poll %s", poll_id)
    return None


async def cast_poll_vote(
    poll_id: str,
    option_key: str,
    *,
    max_user_id: int,
    reporter_name: str,
    reporter_phone: str | None,
    reporter_district: str | None,
) -> tuple[bool, str, bool]:
    """Returns (ok, message, request_screenshot_after_vote)."""
    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        return False, "Голосование временно недоступно.", False

    url = f"{base}/api/internal/max-bot/polls/{poll_id}/vote"
    payload = {
        "maxUserId": str(max_user_id),
        "optionKey": option_key,
        "reporterName": reporter_name,
        "reporterPhone": reporter_phone,
        "reporterDistrict": reporter_district,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            if response.status_code == 201:
                try:
                    body = response.json()
                except Exception:
                    body = {}
                label = html.escape(str(body.get("optionLabel") or "ваш вариант"))
                need_shot = bool(body.get("requestScreenshotAfterVote"))
                return True, f"Спасибо! Ваш ответ: <b>{label}</b> учтён.", need_shot
            try:
                err = response.json().get("error", "")
            except Exception:
                err = response.text[:200]
            return False, err or "Не удалось принять ответ.", False
    except Exception:
        logger.exception("Poll vote failed")
        return False, "Не удалось принять ответ. Попробуйте позже.", False
