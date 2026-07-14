from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.registration import (
    handle_bot_started,
    handle_bot_stopped,
    handle_callback,
    handle_message_created,
)


async def process_update(session: AsyncSession, update: dict[str, Any]) -> None:
    update_type = update.get("update_type")
    if update_type == "bot_started":
        await handle_bot_started(session, update)
    elif update_type == "bot_stopped":
        await handle_bot_stopped(session, update)
    elif update_type == "message_created":
        await handle_message_created(session, update)
    elif update_type == "message_callback":
        await handle_callback(session, update)
