"""Тестовый режим Long Polling — только для отладки до настройки webhook."""

import asyncio
import logging

from app.config import settings
from app.database import async_session, init_db
from app.handlers.webhook import process_update
from app.max_client import max_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def poll_updates(marker: int | None = None) -> int | None:
    import httpx

    params: dict = {"limit": 100, "timeout": 30}
    if marker is not None:
        params["marker"] = marker

    async with httpx.AsyncClient(verify=settings.max_api_verify_ssl, timeout=60) as client:
        response = await client.get(
            f"{settings.max_api_base}/updates",
            headers={"Authorization": settings.max_bot_token},
            params=params,
        )
        response.raise_for_status()
        data = response.json()

    updates = data.get("updates") or []
    new_marker = data.get("marker", marker)

    for update in updates:
        async with async_session() as session:
            await process_update(session, update)
            await session.commit()
        logger.info("Processed: %s", update.get("update_type"))

    return new_marker


async def main() -> None:
    await init_db()
    me = await max_client.get_me()
    logger.info("Bot: %s (@%s)", me.get("name"), me.get("username"))

    marker: int | None = None
    while True:
        try:
            marker = await poll_updates(marker)
        except Exception:
            logger.exception("Polling error")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
