from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.services.appeal_attachments import guess_mime

logger = logging.getLogger(__name__)

_API_ERROR_RU = {
    "Required": "Не хватает обязательных данных. Попробуйте отправить ещё раз.",
    "Invalid enum value": "Тематика обращения пока не поддерживается сервером. Обратитесь в администрацию.",
}


def _humanize_api_error(message: str) -> str:
    for key, text in _API_ERROR_RU.items():
        if key in message:
            return text
    return message


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
            detail = _humanize_api_error(data["error"])
    except Exception:
        pass
    logger.warning("admspgo appeal rejected: %s %s", response.status_code, response.text[:300])
    return False, detail


def _appeals_api_config() -> tuple[str, str] | None:
    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        return None
    return base, token


async def fetch_open_appeals(*, limit: int = 15) -> tuple[list[dict[str, Any]], str | None]:
    config = _appeals_api_config()
    if not config:
        return [], "Сервис обращений временно недоступен."

    base, token = config
    url = f"{base}/api/internal/max-bot/appeals/queue"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, params={"limit": limit}, headers=headers)
    except Exception:
        logger.exception("Failed to GET appeals queue from admspgo")
        return [], "Не удалось загрузить список обращений."

    if response.status_code != 200:
        return [], "Не удалось загрузить список обращений."

    data = response.json()
    appeals = data.get("appeals") if isinstance(data, dict) else None
    if not isinstance(appeals, list):
        return [], "Некорректный ответ сервиса обращений."
    return appeals, None


async def fetch_appeal(appeal_id: str) -> tuple[dict[str, Any] | None, str | None]:
    config = _appeals_api_config()
    if not config:
        return None, "Сервис обращений временно недоступен."

    base, token = config
    url = f"{base}/api/internal/max-bot/appeals/{appeal_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
    except Exception:
        logger.exception("Failed to GET appeal %s from admspgo", appeal_id)
        return None, "Не удалось загрузить обращение."

    if response.status_code == 404:
        return None, "Обращение не найдено."
    if response.status_code != 200:
        return None, "Не удалось загрузить обращение."

    data = response.json()
    appeal = data.get("appeal") if isinstance(data, dict) else None
    if not isinstance(appeal, dict):
        return None, "Некорректный ответ сервиса обращений."
    return appeal, None


async def update_appeal(
    appeal_id: str,
    *,
    status: str,
    response: str | None = None,
) -> tuple[bool, str | None]:
    config = _appeals_api_config()
    if not config:
        return False, "Сервис обращений временно недоступен."

    base, token = config
    url = f"{base}/api/internal/max-bot/appeals/{appeal_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {"status": status}
    if response is not None:
        payload["response"] = response

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response_obj = await client.patch(url, json=payload, headers=headers)
    except Exception:
        logger.exception("Failed to PATCH appeal %s on admspgo", appeal_id)
        return False, "Не удалось сохранить изменения."

    if response_obj.status_code == 404:
        return False, "Обращение не найдено."
    if response_obj.status_code not in {200, 201}:
        detail = "Не удалось сохранить изменения."
        try:
            data = response_obj.json()
            if isinstance(data, dict) and isinstance(data.get("error"), str):
                detail = data["error"]
        except Exception:
            pass
        return False, detail
    return True, None


async def download_attachment(meta: dict[str, Any]) -> tuple[str, bytes, str] | None:
    from app.services.appeal_attachments import download_max_attachment

    result = await download_max_attachment(meta)
    if result:
        return result
    url = meta.get("url")
    if url:
        logger.warning("Failed to download MAX attachment from %s", str(url)[:120])
    return None
