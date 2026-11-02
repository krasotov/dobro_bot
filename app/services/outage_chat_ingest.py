from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.max_client import max_client
from app.services.channel_news import extract_message_id, extract_message_link, resolve_update_chat_id

logger = logging.getLogger(__name__)

def outage_source_chat_id() -> int | None:
    raw = getattr(settings, "outage_source_chat_id", None)
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def is_outage_source_message(update: dict[str, Any]) -> bool:
    configured = outage_source_chat_id()
    if configured is None:
        return False
    update_chat_id = resolve_update_chat_id(update)
    return update_chat_id is not None and int(update_chat_id) == int(configured)


def _extract_sender(update: dict[str, Any]) -> tuple[str | None, int | None]:
    message = update.get("message") or {}
    sender = message.get("sender") or update.get("user") or {}
    if not isinstance(sender, dict):
        return None, None
    name_parts = [
        str(sender.get("first_name") or "").strip(),
        str(sender.get("last_name") or "").strip(),
        str(sender.get("name") or "").strip(),
        str(sender.get("username") or "").strip(),
    ]
    name = " ".join(part for part in name_parts if part).strip() or None
    raw_id = sender.get("user_id") or sender.get("id")
    try:
        sender_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        sender_id = None
    return name, sender_id


def _format_received_at(timestamp: Any) -> str:
    if isinstance(timestamp, int) and timestamp > 0:
        dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_text(update: dict[str, Any]) -> str:
    message = update.get("message") or {}
    body = message.get("body") or {}
    return (body.get("text") or "").strip()


async def forward_outage_chat_message(update: dict[str, Any]) -> tuple[bool, str | None]:
    message_id = extract_message_id(update.get("message") or {})
    text = _extract_text(update)
    chat_id = resolve_update_chat_id(update)
    if not message_id or not text or chat_id is None:
        return False, "empty message"

    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        logger.warning("Outage chat message not forwarded: ADMSPGO API not configured")
        return False, "api not configured"

    sender_name, sender_max_id = _extract_sender(update)
    message = update.get("message") or {}
    timestamp = update.get("timestamp") or message.get("timestamp")
    received_at = _format_received_at(timestamp)

    payload: dict[str, Any] = {
        "chatId": chat_id,
        "messageId": message_id,
        "text": text[:8000],
        "receivedAt": received_at,
    }
    if sender_name:
        payload["senderName"] = sender_name
    if sender_max_id is not None:
        payload["senderMaxUserId"] = sender_max_id

    link_meta = extract_message_link(message)
    if link_meta.get("replyToMessageId"):
        payload["replyToMessageId"] = link_meta["replyToMessageId"]
    if link_meta.get("linkType"):
        payload["linkType"] = link_meta["linkType"]
    if link_meta.get("quotedText"):
        payload["quotedText"] = link_meta["quotedText"]

    url = f"{base}/api/internal/max-bot/outage-messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.post(url, json=payload, headers=headers)
    except Exception:
        logger.exception("Failed to POST outage chat message to admspgo")
        return False, "network error"

    if response.status_code in {200, 201}:
        return True, None

    logger.warning(
        "admspgo outage ingest rejected: %s %s",
        response.status_code,
        response.text[:300],
    )
    return False, response.text[:200] or f"HTTP {response.status_code}"


async def try_ingest_outage_chat_message(_session: AsyncSession, update: dict[str, Any]) -> bool:
    if not is_outage_source_message(update):
        return False

    ok, err = await forward_outage_chat_message(update)
    if ok:
        logger.info(
            "Outage chat message ingested: chat_id=%s message_id=%s",
            resolve_update_chat_id(update),
            extract_message_id(update.get("message") or {}),
        )
    else:
        logger.warning("Outage chat ingest skipped/failed: %s", err)

    return True


def _wrap_message_as_update(message: dict[str, Any]) -> dict[str, Any]:
    timestamp = message.get("timestamp")
    return {
        "timestamp": timestamp,
        "message": message,
        "update_type": "message_created",
    }


async def get_outage_source_status() -> dict[str, Any]:
    chat_id = outage_source_chat_id()
    if chat_id is None:
        return {
            "chatId": None,
            "ready": False,
            "isMember": False,
            "isAdmin": False,
            "title": None,
            "note": "OUTAGE_SOURCE_CHAT_ID не настроен.",
        }

    title: str | None = None
    is_member = False
    is_admin = False
    note = (
        "Для получения сообщений из группы MAX бот «Доброчат» должен быть администратором. "
        "Сейчас webhook и опрос API не видят сообщения."
    )

    try:
        chat = await max_client.get_chat(chat_id)
        if isinstance(chat, dict):
            raw_title = chat.get("title")
            title = str(raw_title).strip() if isinstance(raw_title, str) and raw_title.strip() else None
    except Exception:
        logger.exception("Failed to fetch outage source chat %s", chat_id)

    try:
        data = await max_client._request("GET", f"/chats/{chat_id}/members")
        members = data.get("members") if isinstance(data, dict) else None
        if isinstance(members, list):
            for member in members:
                if not isinstance(member, dict) or not member.get("is_bot"):
                    continue
                is_member = True
                is_admin = bool(member.get("is_admin"))
                break
    except Exception:
        logger.exception("Failed to fetch outage source chat members %s", chat_id)

    if is_member and is_admin:
        note = (
            "Бот является администратором группы. Новые сообщения должны поступать через webhook "
            "и резервный опрос API."
        )

    return {
        "chatId": chat_id,
        "title": title,
        "isMember": is_member,
        "isAdmin": is_admin,
        "ready": is_member and is_admin,
        "note": note,
    }


async def poll_outage_chat_messages() -> int:
    chat_id = outage_source_chat_id()
    if chat_id is None:
        return 0

    status = await get_outage_source_status()
    if not status.get("ready"):
        return 0

    try:
        data = await max_client._request("GET", "/messages", params={"chat_id": chat_id, "count": 30})
    except Exception:
        logger.exception("Failed to poll outage chat messages for chat_id=%s", chat_id)
        return 0

    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list) or not messages:
        return 0

    forwarded = 0
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        update = _wrap_message_as_update(message)
        if not is_outage_source_message(update):
            continue
        ok, err = await forward_outage_chat_message(update)
        if ok:
            forwarded += 1
        elif err and err != "empty message":
            logger.debug("Outage chat poll skipped message: %s", err)

    if forwarded:
        logger.info("Outage chat poll forwarded %s messages from chat_id=%s", forwarded, chat_id)
    return forwarded
