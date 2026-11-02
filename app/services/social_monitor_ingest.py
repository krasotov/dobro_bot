from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.max_client import max_client
from app.services.channel_news import extract_message_id, extract_message_link, resolve_update_chat_id
from app.services.outage_chat_ingest import is_outage_source_message

logger = logging.getLogger(__name__)

_sources_cache: set[int] = set()
_sources_cache_at: float = 0.0
_SOURCES_CACHE_TTL = 300.0


def _extract_sender(update: dict[str, Any]) -> str | None:
    message = update.get("message") or {}
    sender = message.get("sender") or update.get("user") or {}
    if not isinstance(sender, dict):
        return None
    name_parts = [
        str(sender.get("first_name") or "").strip(),
        str(sender.get("last_name") or "").strip(),
        str(sender.get("name") or "").strip(),
        str(sender.get("username") or "").strip(),
    ]
    name = " ".join(part for part in name_parts if part).strip()
    return name or None


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


async def _fetch_enabled_chat_ids() -> set[int]:
    global _sources_cache, _sources_cache_at

    now = time.monotonic()
    if _sources_cache and now - _sources_cache_at < _SOURCES_CACHE_TTL:
        return set(_sources_cache)

    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        return set()

    url = f"{base}/api/internal/max-bot/social-sources"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
    except Exception:
        logger.exception("Failed to fetch social monitor sources from admspgo")
        return set(_sources_cache)

    if response.status_code != 200:
        logger.warning("social-sources rejected: %s %s", response.status_code, response.text[:200])
        return set(_sources_cache)

    data = response.json()
    sources = data.get("sources") if isinstance(data, dict) else None
    chat_ids: set[int] = set()
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            if source.get("platform") != "MAX" or not source.get("isEnabled", True):
                continue
            raw_id = source.get("externalId")
            try:
                chat_ids.add(int(str(raw_id).strip()))
            except (TypeError, ValueError):
                continue

    _sources_cache = chat_ids
    _sources_cache_at = now
    return set(chat_ids)


async def is_social_monitor_message(update: dict[str, Any]) -> bool:
    if is_outage_source_message(update):
        return False

    chat_id = resolve_update_chat_id(update)
    if chat_id is None:
        return False

    enabled = await _fetch_enabled_chat_ids()
    return int(chat_id) in enabled


async def forward_social_monitor_message(update: dict[str, Any]) -> tuple[bool, str | None]:
    message_id = extract_message_id(update.get("message") or {})
    text = _extract_text(update)
    chat_id = resolve_update_chat_id(update)
    if not message_id or not text or chat_id is None:
        return False, "empty message"

    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        logger.warning("Social monitor message not forwarded: ADMSPGO API not configured")
        return False, "api not configured"

    sender_name = _extract_sender(update)
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

    link_meta = extract_message_link(message if isinstance(message, dict) else {})
    reply_to = (link_meta.get("replyToMessageId") or "").strip()
    link_type = (link_meta.get("linkType") or "").strip().lower()
    if reply_to:
        payload["replyToMessageId"] = reply_to
    if link_type in {"reply", "forward"}:
        payload["linkType"] = link_type

    url = f"{base}/api/internal/max-bot/social-items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.post(url, json=payload, headers=headers)
    except Exception:
        logger.exception("Failed to POST social monitor message to admspgo")
        return False, "network error"

    if response.status_code in {200, 201}:
        return True, None

    logger.warning(
        "admspgo social monitor ingest rejected: %s %s",
        response.status_code,
        response.text[:300],
    )
    return False, response.text[:200] or f"HTTP {response.status_code}"


async def try_ingest_social_monitor_message(_session: AsyncSession, update: dict[str, Any]) -> bool:
    if not await is_social_monitor_message(update):
        return False

    ok, err = await forward_social_monitor_message(update)
    if ok:
        logger.info(
            "Social monitor message ingested: chat_id=%s message_id=%s",
            resolve_update_chat_id(update),
            extract_message_id(update.get("message") or {}),
        )
    else:
        logger.warning("Social monitor ingest skipped/failed: %s", err)

    return True


def _wrap_message_as_update(message: dict[str, Any]) -> dict[str, Any]:
    timestamp = message.get("timestamp")
    return {
        "timestamp": timestamp,
        "message": message,
        "update_type": "message_created",
    }


async def poll_social_monitor_messages() -> int:
    enabled = await _fetch_enabled_chat_ids()
    if not enabled:
        return 0

    forwarded = 0
    for chat_id in enabled:
        try:
            data = await max_client._request("GET", "/messages", params={"chat_id": chat_id, "count": 20})
        except Exception:
            logger.exception("Failed to poll social monitor messages for chat_id=%s", chat_id)
            continue

        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list) or not messages:
            continue

        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            update = _wrap_message_as_update(message)
            if not await is_social_monitor_message(update):
                continue
            ok, err = await forward_social_monitor_message(update)
            if ok:
                forwarded += 1
            elif err and err != "empty message":
                logger.debug("Social monitor poll skipped message: %s", err)

    if forwarded:
        logger.info("Social monitor poll forwarded %s messages", forwarded)
    return forwarded
