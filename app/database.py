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
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS address_street VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS address_house VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS address_apartment VARCHAR(32)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS address_latitude DOUBLE PRECISION",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS address_longitude DOUBLE PRECISION",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS districts TEXT",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS image_token VARCHAR(512)",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS image_url TEXT",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS image_filename VARCHAR(255)",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS source_chat_id BIGINT",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS source_message_id VARCHAR(128)",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS auto_from_channel BOOLEAN DEFAULT FALSE",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_broadcasts_source_message_id ON broadcasts (source_message_id) WHERE source_message_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_users_district ON users (district)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS appeal_flow_state VARCHAR(16) DEFAULT 'none'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS appeal_draft_subject VARCHAR(200)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS appeal_draft_message TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS appeal_draft_attachments TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS appeal_draft_category VARCHAR(16) DEFAULT 'general'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_flow_state VARCHAR(16) DEFAULT 'none'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS topic_subscriptions TEXT",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS topics TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS audience_roles TEXT",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS audience_roles TEXT",
    "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS poll_id VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_screenshot_broadcast_id INTEGER REFERENCES broadcasts(id)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_poll_id VARCHAR(64)",
    "ALTER TABLE screenshots ADD COLUMN IF NOT EXISTS poll_id VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS transport_flow_state VARCHAR(16) DEFAULT 'none'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS transport_draft_date VARCHAR(10)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS transport_draft_time VARCHAR(5)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS transport_draft_destination VARCHAR(500)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS appeal_operator_flow_state VARCHAR(16) DEFAULT 'none'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS appeal_operator_appeal_id VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS appeal_operator_target_status VARCHAR(16)",
]


async def ensure_news_channel_source() -> None:
    from sqlalchemy import select

    from app.models import NewsChannelSource

    async with async_session() as session:
        result = await session.execute(select(NewsChannelSource.id).limit(1))
        if result.scalar_one_or_none() is not None:
            return
        session.add(
            NewsChannelSource(
                channel_slug="OErohanovaSP",
                channel_url="https://max.ru/OErohanovaSP",
                title="Канал главы Сергиево-Посадского ГО",
            )
        )
        await session.commit()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    for stmt in MIGRATIONS:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
        except Exception as exc:
            logger.warning("Migration skipped: %s (%s)", stmt[:80], exc)

    await ensure_news_channel_source()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
