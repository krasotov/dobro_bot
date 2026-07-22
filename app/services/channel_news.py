from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Broadcast, NewsChannelSource

logger = logging.getLogger(__name__)


async def get_news_channel_source(session: AsyncSession) -> NewsChannelSource | None:
    result = await session.execute(select(NewsChannelSource).limit(1))
    return result.scalar_one_or_none()


def effective_source_chat_id(source: NewsChannelSource | None) -> int | None:
    if settings.news_source_chat_id:
        return settings.news_source_chat_id
    if source and source.chat_id:
        return source.chat_id
    return None


def resolve_update_chat_id(update: dict[str, Any]) -> int | None:
    chat_id = update.get("chat_id")
    if chat_id is not None:
        return int(chat_id)

    message = update.get("message") or {}
    recipient = message.get("recipient") or {}
    recipient_chat_id = recipient.get("chat_id")
    if recipient_chat_id is not None:
        return int(recipient_chat_id)
    return None


def extract_message_id(message: dict[str, Any]) -> str | None:
    body = message.get("body") or {}
    for key in ("mid", "message_id", "chat_message_id"):
        value = body.get(key) or message.get(key)
        if value:
            return str(value)
    return None


def extract_image_payload(attachments: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    for attachment in attachments:
        if attachment.get("type") not in {"image", "photo", "file"}:
            continue
        payload = attachment.get("payload") or {}
        token = payload.get("token")
        if token:
            return str(token), None
        url = payload.get("url") or payload.get("file_url")
        if url:
            return None, str(url)
    return None, None


async def bind_channel_on_bot_added(session: AsyncSession, update: dict[str, Any]) -> None:
    if not update.get("is_channel"):
        return

    chat_id = update.get("chat_id")
    if chat_id is None:
        return

    source = await get_news_channel_source(session)
    if not source:
        source = NewsChannelSource(
            channel_slug="OErohanovaSP",
            channel_url="https://max.ru/OErohanovaSP",
            title="Канал главы Сергиево-Посадского ГО",
        )
        session.add(source)
        await session.flush()

    if source.chat_id and int(source.chat_id) != int(chat_id):
        logger.warning(
            "Bot added to channel %s, but news source already bound to %s",
            chat_id,
            source.chat_id,
        )
        return

    source.chat_id = int(chat_id)
    source.is_enabled = True
    logger.info("News source channel bound to chat_id=%s", chat_id)


async def is_channel_source_message(session: AsyncSession, update: dict[str, Any]) -> bool:
    source = await get_news_channel_source(session)
    if not source or not source.is_enabled:
        return False
    configured_chat_id = effective_source_chat_id(source)
    if configured_chat_id is None:
        return False
    update_chat_id = resolve_update_chat_id(update)
    return update_chat_id == configured_chat_id


async def try_relay_channel_post(session: AsyncSession, update: dict[str, Any]) -> int | None:
    if not await is_channel_source_message(session, update):
        return None

    message = update.get("message") or {}
    message_id = extract_message_id(message)
    if not message_id:
        logger.warning("Channel post without message id, skip relay: %s", update)
        return None

    existing = await session.scalar(
        select(Broadcast.id).where(Broadcast.source_message_id == message_id)
    )
    if existing:
        logger.info("Channel post %s already relayed", message_id)
        return None

    body = message.get("body") or {}
    text = (body.get("text") or "").strip()
    image_token, image_url = extract_image_payload(body.get("attachments") or [])

    if not text and not image_token and not image_url:
        logger.info("Channel post %s has no text or image, skip relay", message_id)
        return None

    if not text:
        text = "Новость из официального канала."

    configured_chat_id = effective_source_chat_id(await get_news_channel_source(session))
    broadcast = Broadcast(
        text=text,
        image_token=image_token,
        image_url=image_url,
        image_filename="channel_post" if (image_token or image_url) else None,
        source_chat_id=configured_chat_id,
        source_message_id=message_id,
        auto_from_channel=True,
    )
    session.add(broadcast)
    await session.flush()
    logger.info(
        "Scheduled auto-relay broadcast %s for channel post %s",
        broadcast.id,
        message_id,
    )
    return broadcast.id
