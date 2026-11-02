from __future__ import annotations

import logging
from typing import Any

from app.handlers.webhook import process_update
from app.max_client import max_client

logger = logging.getLogger(__name__)

_marker: int | None = None


async def poll_max_updates_once() -> int:
    """Long polling GET /updates — подхватывает callback, если webhook не дошёл."""
    global _marker
    from app.config import settings

    params: dict[str, Any] = {
        "limit": 100,
        "timeout": min(25, max(5, int(getattr(settings, "max_updates_poll_timeout_sec", 20)))),
    }
    if _marker is not None:
        params["marker"] = _marker

    data = await max_client.get_updates(**params)
    updates = data.get("updates") if isinstance(data, dict) else None
    if not isinstance(updates, list):
        return 0

    new_marker = data.get("marker")
    if new_marker is not None:
        _marker = int(new_marker)

    if not updates:
        return 0

    from app.database import async_session

    processed = 0
    async with async_session() as session:
        for update in updates:
            if not isinstance(update, dict):
                continue
            try:
                await process_update(session, update)
                await session.commit()
                processed += 1
            except Exception:
                await session.rollback()
                logger.exception(
                    "Failed to process polled update: %s",
                    update.get("update_type"),
                )
    if processed:
        logger.info("MAX /updates poll processed %s update(s)", processed)
    return processed
