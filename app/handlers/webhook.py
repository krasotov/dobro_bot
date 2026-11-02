from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.bot_chats import (
    mark_bot_chat_removed,
    maybe_track_chat_from_message,
    upsert_bot_chat_from_update,
)
from app.services.channel_news import (
    bind_channel_on_bot_added,
    is_channel_source_message,
    try_relay_channel_post,
)
from app.services.outage_chat_ingest import try_ingest_outage_chat_message
from app.services.social_monitor_ingest import try_ingest_social_monitor_message
from app.services.registration import (
    handle_bot_started,
    handle_bot_stopped,
    handle_callback,
    handle_message_created,
)

logger = logging.getLogger(__name__)


async def process_update(session: AsyncSession, update: dict[str, Any]) -> int | None:
    update_type = update.get("update_type")

    if update_type in {"message_created", "message_callback"}:
        from app.services.update_dedupe import is_duplicate_update, mark_update_seen

        if is_duplicate_update(update):
            logger.info("Skip duplicate update: %s", update_type)
            return None
        mark_update_seen(update)

    logger.info("Processing update: %s", update_type)

    if update_type == "bot_added":
        await upsert_bot_chat_from_update(session, update)
        await bind_channel_on_bot_added(session, update)
        return None

    if update_type == "bot_removed":
        await mark_bot_chat_removed(session, update)
        return None

    if update_type == "bot_started":
        await handle_bot_started(session, update)
        return None

    if update_type == "bot_stopped":
        await handle_bot_stopped(session, update)
        return None

    if update_type == "message_created":
        await maybe_track_chat_from_message(session, update)
        if await try_ingest_outage_chat_message(session, update):
            return None
        if await try_ingest_social_monitor_message(session, update):
            return None
        if await is_channel_source_message(session, update):
            return await try_relay_channel_post(session, update)
        await handle_message_created(session, update)
        return None

    if update_type == "message_callback":
        callback = update.get("callback") if isinstance(update.get("callback"), dict) else {}
        cb_user = callback.get("user") if isinstance(callback.get("user"), dict) else {}
        logger.info(
            "message_callback uid=%s payload=%r cid=%s",
            cb_user.get("user_id"),
            callback.get("payload"),
            callback.get("callback_id"),
        )
        await handle_callback(session, update)
        return None

    logger.info("Unhandled update type: %s", update_type)
    return None
