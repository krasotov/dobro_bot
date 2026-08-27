from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import max_client, topic_subscriptions_keyboard
from app.models import User
from app.services.admspgo_topics import fetch_published_topics
from app.texts import TOPICS_EMPTY, TOPICS_INTRO, TOPICS_UNAVAILABLE


async def ensure_default_subscriptions(user: User) -> None:
    if user.get_topic_subscription_list() is not None:
        return
    topics = await fetch_published_topics()
    if not topics:
        return
    user.set_topic_subscription_list([str(t["slug"]) for t in topics])


async def send_subscriptions_panel(session: AsyncSession, user: User) -> None:
    topics = await fetch_published_topics()
    if topics is None:
        await max_client.send_message(user.max_user_id, TOPICS_UNAVAILABLE)
        return
    if not topics:
        await max_client.send_message(user.max_user_id, TOPICS_EMPTY)
        return

    if user.get_topic_subscription_list() is None:
        user.set_topic_subscription_list([str(t["slug"]) for t in topics])
        await session.commit()

    subscribed = set(user.get_topic_subscription_list() or [])
    await max_client.send_message(
        user.max_user_id,
        TOPICS_INTRO,
        attachments=topic_subscriptions_keyboard(topics, subscribed),
    )


async def try_handle_topic_callback(
    session: AsyncSession,
    user: User,
    payload: str,
) -> bool:
    if payload == "menu_subscriptions":
        if not user.is_registered:
            await max_client.send_message(
                user.max_user_id,
                "Подписки доступны после регистрации в боте.",
            )
            return True
        await send_subscriptions_panel(session, user)
        return True

    if not payload.startswith("topic_toggle:"):
        return False

    if not user.is_registered:
        return True

    slug = payload.split(":", 1)[1].strip()
    topics = await fetch_published_topics()
    if not topics:
        await max_client.send_message(user.max_user_id, TOPICS_UNAVAILABLE)
        return True

    valid_slugs = {str(t["slug"]) for t in topics}
    if slug not in valid_slugs:
        return True

    current = user.get_topic_subscription_list()
    if current is None:
        current = list(valid_slugs)
    subscribed = set(current)
    if slug in subscribed:
        subscribed.discard(slug)
    else:
        subscribed.add(slug)

    user.set_topic_subscription_list(sorted(subscribed))
    await session.commit()

    await max_client.send_message(
        user.max_user_id,
        TOPICS_INTRO,
        attachments=topic_subscriptions_keyboard(topics, subscribed),
    )
    return True
