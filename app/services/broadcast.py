from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import MaxApiError, max_client, poll_vote_keyboard
from app.models import Broadcast, BroadcastDelivery, User
from app.services.admspgo_polls import fetch_poll_by_id
from app.services.audience_roles import (
    get_broadcast_audience_role_list,
    user_matches_broadcast_audience,
)
from app.texts import BROADCAST_SCREENSHOT_HINT

logger = logging.getLogger(__name__)


async def _deliver_broadcast_message(
    user: User,
    broadcast: Broadcast,
    poll: dict | None,
) -> None:
    if broadcast.auto_from_channel and broadcast.source_message_id and not broadcast.poll_id:
        try:
            await max_client.forward_message(user.max_user_id, broadcast.source_message_id)
            return
        except MaxApiError as exc:
            logger.warning(
                "Forward failed for user %s broadcast %s, fallback to copy: %s",
                user.max_user_id,
                broadcast.id,
                exc.message[:300],
            )

    text = broadcast.text
    if broadcast.request_screenshot:
        text = f"{text.rstrip()}\n\n{BROADCAST_SCREENSHOT_HINT}"

    extra_attachments = None
    if poll and poll.get("options"):
        import html as html_mod

        question = html_mod.escape(str(poll.get("question") or "").strip())
        if question:
            text = f"{text.rstrip()}\n\n<b>{question}</b>" if text.strip() else f"<b>{question}</b>"
        extra_attachments = poll_vote_keyboard(
            str(poll["id"]),
            poll.get("options") or [],
            broadcast_id=broadcast.id,
        )

    await max_client.send_message_with_image(
        user.max_user_id,
        text,
        image_token=broadcast.image_token,
        image_url=broadcast.image_url,
        extra_attachments=extra_attachments,
    )


async def send_broadcast(
    session: AsyncSession,
    broadcast_id: int,
    *,
    delay_seconds: float = 0.04,
) -> Broadcast:
    result = await session.execute(select(Broadcast).where(Broadcast.id == broadcast_id))
    broadcast = result.scalar_one()

    poll_data = None
    if broadcast.poll_id:
        poll_data = await fetch_poll_by_id(broadcast.poll_id)

    districts = broadcast.get_district_list()
    broadcast_topics = broadcast.get_topic_list()
    broadcast_audience = get_broadcast_audience_role_list(broadcast)
    query = select(User).where(User.is_registered.is_(True), User.is_active.is_(True))
    if districts:
        query = query.where(User.district.in_(districts))

    users_result = await session.execute(query)
    users = [
        u
        for u in users_result.scalars().all()
        if user_matches_broadcast_topics(u, broadcast_topics)
        and user_matches_broadcast_audience(u, broadcast_audience)
    ]

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
            await _deliver_broadcast_message(user, broadcast, poll_data)
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


def encode_districts(districts: list[str]) -> str | None:
    cleaned = [d.strip() for d in districts if d and d.strip()]
    if not cleaned:
        return None
    return json.dumps(cleaned, ensure_ascii=False)


def encode_topics(topics: list[str]) -> str | None:
    cleaned = [t.strip() for t in topics if t and t.strip()]
    if not cleaned:
        return None
    return json.dumps(cleaned, ensure_ascii=False)


def user_matches_broadcast_topics(user: User, broadcast_topics: list[str]) -> bool:
    if not broadcast_topics:
        return True
    user_topics = user.get_topic_subscription_list()
    if user_topics is None:
        return True
    return bool(set(user_topics) & set(broadcast_topics))
