from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import MaxApiError, max_client
from app.models import Broadcast, BroadcastDelivery, User


async def send_broadcast(
    session: AsyncSession,
    broadcast_id: int,
    *,
    delay_seconds: float = 0.04,
) -> Broadcast:
    result = await session.execute(select(Broadcast).where(Broadcast.id == broadcast_id))
    broadcast = result.scalar_one()
    users_result = await session.execute(
        select(User).where(User.is_registered.is_(True), User.is_active.is_(True))
    )
    users = users_result.scalars().all()

    broadcast.total_recipients = len(users)
    sent = 0
    failed = 0

    for user in users:
        delivery = BroadcastDelivery(
            broadcast_id=broadcast.id,
            user_id=user.id,
            status="pending",
        )
        session.add(delivery)
        await session.flush()

        try:
            await max_client.send_message(user.max_user_id, broadcast.text)
            delivery.status = "sent"
            delivery.sent_at = datetime.now(timezone.utc)
            sent += 1
        except MaxApiError as exc:
            delivery.status = "failed"
            delivery.error_message = exc.message[:1000]
            failed += 1

        await session.commit()
        await asyncio.sleep(delay_seconds)

    broadcast.sent_count = sent
    broadcast.failed_count = failed
    broadcast.sent_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(broadcast)
    return broadcast
