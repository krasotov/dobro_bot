from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import max_client
from app.models import BotChat

logger = logging.getLogger(__name__)

CHAT_TYPE_LABELS = {
    "chat": "Группа",
    "channel": "Канал",
    "dialog": "Диалог",
}

STATUS_LABELS = {
    "active": "Активен",
    "removed": "Бот удалён",
    "left": "Бот вышел",
    "closed": "Закрыт",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_update_chat_id(update: dict[str, Any]) -> int | None:
    from app.services.channel_news import resolve_update_chat_id as resolve_chat_id

    return resolve_chat_id(update)


def _chat_type_from_update(update: dict[str, Any]) -> str:
    if update.get("is_channel"):
        return "channel"
    return "chat"


async def fetch_chat_from_api(chat_id: int) -> dict[str, Any] | None:
    try:
        data = await max_client.get_chat(chat_id)
    except Exception:
        logger.exception("Failed to fetch chat %s from MAX API", chat_id)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _apply_api_payload(chat: BotChat, payload: dict[str, Any]) -> None:
    chat_type = payload.get("type")
    if chat_type:
        chat.chat_type = str(chat_type)
        chat.is_channel = chat_type == "channel"
    chat.status = str(payload.get("status") or chat.status or "active")
    title = payload.get("title")
    chat.title = str(title).strip() if isinstance(title, str) and title.strip() else chat.title
    link = payload.get("link")
    chat.link = str(link).strip() if isinstance(link, str) and link.strip() else chat.link
    description = payload.get("description")
    chat.description = str(description).strip() if isinstance(description, str) else chat.description
    participants = payload.get("participants_count")
    if isinstance(participants, int):
        chat.participants_count = participants
    is_public = payload.get("is_public")
    if isinstance(is_public, bool):
        chat.is_public = is_public
    last_event = payload.get("last_event_time")
    if isinstance(last_event, int) and last_event > 0:
        chat.last_event_at = datetime.fromtimestamp(last_event / 1000, tz=timezone.utc)
    chat.last_synced_at = _utcnow()


async def upsert_bot_chat_from_update(session: AsyncSession, update: dict[str, Any]) -> BotChat | None:
    chat_id = resolve_update_chat_id(update)
    if chat_id is None:
        return None

    chat = await session.scalar(select(BotChat).where(BotChat.chat_id == chat_id))
    if not chat:
        chat = BotChat(chat_id=chat_id)
        session.add(chat)

    chat.chat_type = _chat_type_from_update(update)
    chat.status = "active"
    chat.is_channel = bool(update.get("is_channel"))
    chat.added_at = chat.added_at or _utcnow()
    chat.removed_at = None

    api_payload = await fetch_chat_from_api(chat_id)
    if api_payload:
        _apply_api_payload(chat, api_payload)
    else:
        chat.last_synced_at = _utcnow()

    return chat


async def mark_bot_chat_removed(session: AsyncSession, update: dict[str, Any]) -> BotChat | None:
    chat_id = resolve_update_chat_id(update)
    if chat_id is None:
        return None

    chat = await session.scalar(select(BotChat).where(BotChat.chat_id == chat_id))
    if not chat:
        chat = BotChat(
            chat_id=chat_id,
            chat_type=_chat_type_from_update(update),
            is_channel=bool(update.get("is_channel")),
            added_at=_utcnow(),
        )
        session.add(chat)

    chat.status = "removed"
    chat.removed_at = _utcnow()
    chat.last_synced_at = _utcnow()
    return chat


async def refresh_bot_chat(session: AsyncSession, chat: BotChat) -> BotChat:
    payload = await fetch_chat_from_api(chat.chat_id)
    if payload:
        _apply_api_payload(chat, payload)
    else:
        chat.last_synced_at = _utcnow()
    return chat


async def discover_chats_from_api(session: AsyncSession) -> tuple[int, int, str | None]:
    """Пробует GET /chats (legacy) и сохраняет все найденные чаты бота."""
    discovered = 0
    total = 0
    marker: int | None = None
    error: str | None = None

    for _page in range(20):
        params: dict[str, Any] = {"count": 100}
        if marker is not None:
            params["marker"] = marker
        try:
            data = await max_client._request("GET", "/chats", params=params)
        except Exception as exc:
            logger.exception("GET /chats failed")
            error = str(exc)
            break
        if not isinstance(data, dict):
            error = "Некорректный ответ MAX API"
            break

        items = data.get("chats") or []
        if not isinstance(items, list):
            error = "Некорректный список чатов"
            break

        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("chat_id")
            if raw_id is None:
                continue
            chat_id = int(raw_id)
            total += 1
            chat = await session.scalar(select(BotChat).where(BotChat.chat_id == chat_id))
            is_new = chat is None
            if is_new:
                chat = BotChat(chat_id=chat_id)
                session.add(chat)
                chat.added_at = _utcnow()
            _apply_api_payload(chat, item)
            chat.removed_at = None if chat.status == "active" else chat.removed_at
            chat.last_synced_at = _utcnow()
            if is_new:
                discovered += 1

        next_marker = data.get("marker")
        if next_marker is None or not items:
            break
        marker = int(next_marker)

    return discovered, total, error


async def maybe_track_chat_from_message(session: AsyncSession, update: dict[str, Any]) -> None:
    """Подхватывает chat_id из входящих сообщений (группы, где бот уже был до учёта)."""
    chat_id = resolve_update_chat_id(update)
    if chat_id is None:
        return
    message = update.get("message") or {}
    recipient = message.get("recipient") or {}
    chat_type = recipient.get("chat_type") or recipient.get("type")
    if chat_type == "dialog":
        return
    existing = await session.scalar(select(BotChat).where(BotChat.chat_id == chat_id))
    if existing and existing.title and existing.last_synced_at:
        return
    await upsert_bot_chat_from_update(
        session,
        {
            "chat_id": chat_id,
            "is_channel": bool(update.get("is_channel") or chat_type == "channel"),
        },
    )


async def refresh_all_bot_chats(session: AsyncSession) -> tuple[int, int]:
    chats = (await session.scalars(select(BotChat).order_by(BotChat.added_at.desc()))).all()
    ok_count = 0
    for chat in chats:
        payload = await fetch_chat_from_api(chat.chat_id)
        if payload:
            _apply_api_payload(chat, payload)
            ok_count += 1
        else:
            chat.last_synced_at = _utcnow()
    return ok_count, len(chats)


async def ensure_known_chat(session: AsyncSession, chat_id: int, *, is_channel: bool = False) -> BotChat:
    chat = await session.scalar(select(BotChat).where(BotChat.chat_id == chat_id))
    if chat:
        return chat
    chat = BotChat(
        chat_id=chat_id,
        chat_type="channel" if is_channel else "chat",
        is_channel=is_channel,
        status="active",
        added_at=_utcnow(),
    )
    session.add(chat)
    payload = await fetch_chat_from_api(chat_id)
    if payload:
        _apply_api_payload(chat, payload)
    return chat


def serialize_bot_chat(chat: BotChat, *, news_source_chat_id: int | None) -> dict[str, Any]:
    chat_type = chat.chat_type or ("channel" if chat.is_channel else "chat")
    status = chat.status or "active"
    return {
        "chatId": chat.chat_id,
        "type": chat_type,
        "typeLabel": CHAT_TYPE_LABELS.get(chat_type, chat_type),
        "status": status,
        "statusLabel": STATUS_LABELS.get(status, status),
        "title": chat.title,
        "link": chat.link,
        "description": chat.description,
        "participantsCount": chat.participants_count,
        "isPublic": chat.is_public,
        "isChannel": chat.is_channel,
        "isNewsSource": news_source_chat_id is not None and int(news_source_chat_id) == int(chat.chat_id),
        "addedAt": chat.added_at.isoformat() if chat.added_at else None,
        "removedAt": chat.removed_at.isoformat() if chat.removed_at else None,
        "lastSyncedAt": chat.last_synced_at.isoformat() if chat.last_synced_at else None,
    }
