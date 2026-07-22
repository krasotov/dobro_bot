from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BroadcastDelivery, Screenshot, User

logger = logging.getLogger(__name__)


async def delete_user_record(session: AsyncSession, user_id: int) -> User | None:
    user = await session.get(User, user_id)
    if not user:
        return None

    await session.execute(delete(Screenshot).where(Screenshot.user_id == user.id))
    await session.execute(delete(BroadcastDelivery).where(BroadcastDelivery.user_id == user.id))
    await session.delete(user)
    await session.commit()
    logger.info("Deleted user id=%s max_user_id=%s", user_id, user.max_user_id)
    return user
