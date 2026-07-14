from collections.abc import AsyncGenerator
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

logger = logging.getLogger(__name__)

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)

MIGRATIONS = [
    "ALTER TYPE registrationstate ADD VALUE IF NOT EXISTS 'awaiting_address_confirm'",
    "ALTER TYPE registrationstate ADD VALUE IF NOT EXISTS 'awaiting_phone_manual'",
    "ALTER TYPE registrationstate ADD VALUE IF NOT EXISTS 'AWAITING_BIRTH_DATE'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS birth_date VARCHAR(10)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS address_normalized TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS district VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS region VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS city VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS settlement VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS address_qc INTEGER",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS districts TEXT",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS image_token VARCHAR(512)",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS image_filename VARCHAR(255)",
    "CREATE INDEX IF NOT EXISTS ix_users_district ON users (district)",
]


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    for stmt in MIGRATIONS:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
        except Exception as exc:
            logger.warning("Migration skipped: %s (%s)", stmt[:80], exc)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
