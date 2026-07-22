from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.channel_news import (
    bind_channel_on_bot_added,
    is_channel_source_message,
    try_relay_channel_post,
)
from app.services.registration import (
    handle_bot_started,
    handle_bot_stopped,
    handle_callback,
    handle_message_created,
)

logger = logging.getLogger(__name__)


async def process_update(session: AsyncSession, update: dict[str, Any]) -> int | None:
    update_type = update.get("update_type")
    logger.info("Processing update: %s", update_type)

    if update_type == "bot_added":
        await bind_channel_on_bot_added(session, update)
        return None

    if update_type == "bot_started":
        await handle_bot_started(session, update)
        return None

    if update_type == "bot_stopped":
        await handle_bot_stopped(session, update)
        return None

    if update_type == "message_created":
        if await is_channel_source_message(session, update):
            return await try_relay_channel_post(session, update)
        await handle_message_created(session, update)
        return None

    if update_type == "message_callback":
        await handle_callback(session, update)
        return None

    logger.info("Unhandled update type: %s", update_type)
    return None
