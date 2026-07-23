from __future__ import annotations

import html
import logging
from typing import Any

import httpx

from app.config import settings
from app.max_client import max_client

logger = logging.getLogger(__name__)

DIRECTORY_COMMANDS = {
    "справочник",
    "контакты",
    "телефоны",
    "directory",
    "contacts",
}

MAX_MESSAGE_LEN = 3800


async def fetch_directory_entries() -> list[dict[str, Any]] | None:
    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        return None

    url = f"{base}/api/internal/max-bot/directory"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if response.status_code != 200:
                logger.warning("directory fetch %s: %s", response.status_code, response.text[:200])
                return None
            data = response.json()
            entries = data.get("entries") if isinstance(data, dict) else None
            if isinstance(entries, list):
                return [e for e in entries if isinstance(e, dict)]
    except Exception:
        logger.exception("Failed to fetch directory from admspgo")
    return None


def _format_entries(entries: list[dict[str, Any]]) -> list[str]:
    chunks: list[str] = []
    current = "<b>Справочник</b>\n\n"
    for entry in entries:
        title = html.escape(str(entry.get("title") or "Без названия"))
        body = html.escape(str(entry.get("body") or "")).replace("\n", "<br>")
        block = f"<b>{title}</b><br>{body}<br><br>"
        if len(current) + len(block) > MAX_MESSAGE_LEN:
            chunks.append(current.rstrip())
            current = block
        else:
            current += block
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


async def send_directory_to_user(max_user_id: int) -> None:
    entries = await fetch_directory_entries()
    if entries is None:
        await max_client.send_message(
            max_user_id,
            "Справочник временно недоступен. Попробуйте позже или обратитесь в администрацию округа.",
        )
        return
    if not entries:
        await max_client.send_message(
            max_user_id,
            "В справочнике пока нет записей. Информация появится после публикации администрацией.",
        )
        return

    for part in _format_entries(entries):
        await max_client.send_message(max_user_id, part)


async def try_directory_command(max_user_id: int, text: str) -> bool:
    if text.lower().strip() not in DIRECTORY_COMMANDS:
        return False
    await send_directory_to_user(max_user_id)
    return True
