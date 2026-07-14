"""Одноразовая нормализация адресов существующих пользователей через DaData."""

import asyncio

from app.database import async_session, init_db
from app.services.address import reprocess_existing_addresses


async def main() -> None:
    await init_db()
    async with async_session() as session:
        count = await reprocess_existing_addresses(session)
        print(f"Обработано адресов: {count}")


if __name__ == "__main__":
    asyncio.run(main())
