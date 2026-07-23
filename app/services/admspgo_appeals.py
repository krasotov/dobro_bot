from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.services.appeal_attachments import guess_mime

logger = logging.getLogger(__name__)


async def submit_max_appeal(
    payload: dict[str, Any],
    files: list[tuple[str, bytes, str]] | None = None,
) -> tuple[bool, str | None]:
    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        logger.error("ADMSPGO_API_URL or ADMSPGO_INTERNAL_TOKEN not configured")
        return False, "Сервис обращений временно недоступен. Попробуйте позже или обратитесь в администрацию."

    url = f"{base}/api/internal/max-bot/appeals"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            if files:
                form_data = {key: str(value) for key, value in payload.items()}
                file_parts = [("files", (name, content, mime)) for name, content, mime in files]
                response = await client.post(url, data=form_data, files=file_parts, headers=headers)
            else:
                response = await client.post(
                    url,
                    json=payload,
                    headers={**headers, "Content-Type": "application/json"},
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


async def download_attachment(meta: dict[str, Any]) -> tuple[str, bytes, str] | None:
    url = meta.get("url")
    if not url or not isinstance(url, str):
        return None
    filename = str(meta.get("filename") or "photo.jpg")[:200]
    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.content
            if len(content) > 10 * 1024 * 1024:
                return None
            mime = response.headers.get("content-type", "").split(";")[0].strip() or guess_mime(filename)
            return filename, content, mime
    except Exception:
        logger.exception("Failed to download MAX attachment from %s", url[:120])
        return None
