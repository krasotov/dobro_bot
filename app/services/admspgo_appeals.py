from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def submit_max_appeal(payload: dict) -> tuple[bool, str | None]:
    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        logger.error("ADMSPGO_API_URL or ADMSPGO_INTERNAL_TOKEN not configured")
        return False, "Сервис обращений временно недоступен. Попробуйте позже или обратитесь в администрацию."

    url = f"{base}/api/internal/max-bot/appeals"
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
    except Exception:
        logger.exception("Failed to POST appeal to admspgo")
        return False, "Не удалось отправить обращение. Попробуйте позже."

    if response.status_code in {200, 201}:
        return True, None

    detail = "Не удалось принять обращение."
    try:
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            detail = data["error"]
    except Exception:
        pass
    logger.warning("admspgo appeal rejected: %s %s", response.status_code, response.text[:300])
    return False, detail
