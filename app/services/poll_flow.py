from __future__ import annotations

import html as html_mod
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import max_client, poll_detail_keyboard, polls_list_keyboard
from app.models import Broadcast, User
from app.services.admspgo_polls import cast_poll_vote, fetch_open_polls, fetch_poll_by_id
from app.services.bot_menu import send_main_menu
from app.texts import (
    POLLS_EMPTY,
    POLLS_NEED_REGISTRATION,
    POLLS_SCREENSHOT_PROMPT,
    POLLS_UNAVAILABLE,
)

_POLL_CACHE: dict[str, dict[str, Any]] = {}


def _format_poll_detail(poll: dict[str, Any]) -> str:
    title = html_mod.escape(str(poll.get("title") or "Опрос"))
    question = html_mod.escape(str(poll.get("question") or ""))
    parts = [f"<b>{title}</b>"]
    if question:
        parts.append(question)
    parts.append("")
    parts.append("Выберите вариант ответа:")
    return "\n".join(parts)


async def send_polls_list(max_user_id: int) -> None:
    polls = await fetch_open_polls()
    if polls is None:
        await max_client.send_message(max_user_id, POLLS_UNAVAILABLE)
        return
    if not polls:
        await max_client.send_message(max_user_id, POLLS_EMPTY)
        return

    _POLL_CACHE.clear()
    for poll in polls[:15]:
        _POLL_CACHE[str(poll["id"])] = poll

    await max_client.send_message(
        max_user_id,
        "Активные опросы — выберите, чтобы ответить:",
        attachments=polls_list_keyboard(polls[:15]),
    )


async def _after_vote(
    session: AsyncSession,
    user: User,
    *,
    need_screenshot: bool,
    broadcast_id: int | None,
    poll_id: str,
) -> None:
    if not need_screenshot:
        await send_main_menu(user.max_user_id, registered=True)
        return

    user.pending_poll_id = poll_id
    user.pending_screenshot_broadcast_id = broadcast_id
    await session.commit()
    await max_client.send_message(user.max_user_id, POLLS_SCREENSHOT_PROMPT)


async def try_handle_poll_callback(
    session: AsyncSession,
    user: User,
    payload: str,
) -> bool:
    if payload == "menu_polls":
        if not user.is_registered:
            await max_client.send_message(user.max_user_id, POLLS_NEED_REGISTRATION)
            return True
        await send_polls_list(user.max_user_id)
        return True

    if payload == "polls_back":
        await send_polls_list(user.max_user_id)
        return True

    if payload.startswith("poll_pick:"):
        if not user.is_registered:
            await max_client.send_message(user.max_user_id, POLLS_NEED_REGISTRATION)
            return True
        poll_id = payload.split(":", 1)[1]
        poll = _POLL_CACHE.get(poll_id) or await fetch_poll_by_id(poll_id)
        if not poll:
            await max_client.send_message(user.max_user_id, "Этот опрос больше недоступен.")
            await send_polls_list(user.max_user_id)
            return True
        _POLL_CACHE[poll_id] = poll
        await max_client.send_message(
            user.max_user_id,
            _format_poll_detail(poll),
            attachments=poll_detail_keyboard(poll_id, poll.get("options") or [], broadcast_id=None),
        )
        return True

    if payload.startswith("poll_vote:"):
        if not user.is_registered:
            await max_client.send_message(user.max_user_id, POLLS_NEED_REGISTRATION)
            return True
        parts = payload.split(":")
        if len(parts) < 4:
            return False
        poll_id = parts[1]
        option_key = parts[2]
        broadcast_part = parts[3]
        broadcast_id: int | None = None
        if broadcast_part.isdigit() and int(broadcast_part) > 0:
            broadcast_id = int(broadcast_part)

        ok, message, need_shot = await cast_poll_vote(
            poll_id,
            option_key,
            max_user_id=user.max_user_id,
            reporter_name=user.fio or user.max_name or "Житель",
            reporter_phone=user.phone,
            reporter_district=user.district,
        )
        await max_client.send_message(user.max_user_id, message)
        if ok:
            poll = _POLL_CACHE.get(poll_id) or await fetch_poll_by_id(poll_id)
            if poll and poll.get("requestScreenshotAfterVote"):
                need_shot = True
            if broadcast_id:
                broadcast = await session.get(Broadcast, broadcast_id)
                if broadcast and broadcast.request_screenshot:
                    need_shot = True
            await _after_vote(
                session,
                user,
                need_screenshot=need_shot,
                broadcast_id=broadcast_id,
                poll_id=poll_id,
            )
        return True

    return False
