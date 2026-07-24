from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import event_detail_keyboard, events_list_keyboard, max_client
from app.models import User
from app.services.admspgo_events import fetch_open_events, register_for_event
from app.services.bot_menu import send_main_menu
from app.texts import (
    EVENTS_EMPTY,
    EVENTS_NEED_REGISTRATION,
    EVENTS_UNAVAILABLE,
)

_EVENT_CACHE: dict[str, dict[str, Any]] = {}


def _format_event_datetime(iso: str | None) -> str:
    if not iso:
        return "дата уточняется"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso


def _format_event_detail(event: dict[str, Any]) -> str:
    import html as html_mod

    title = html_mod.escape(str(event.get("title") or "Мероприятие"))
    when = _format_event_datetime(event.get("eventStartsAt"))
    place = html_mod.escape(str(event.get("eventPlace") or "уточняется"))
    summary = html_mod.escape(str(event.get("summary") or "").strip())
    spots = event.get("spotsLeft")
    limit = event.get("registrationLimit")
    if limit is not None and spots is not None:
        seats = f"Свободно мест: <b>{spots}</b> из {limit}."
    elif limit is not None:
        seats = f"Лимит мест: {limit}."
    else:
        seats = "Количество мест не ограничено."

    parts = [f"<b>{title}</b>", f"Когда: {when}", f"Где: {place}", seats]
    if summary:
        parts.append("")
        parts.append(summary[:600])
    return "\n".join(parts)


async def send_events_list(max_user_id: int) -> None:
    events = await fetch_open_events()
    if events is None:
        await max_client.send_message(max_user_id, EVENTS_UNAVAILABLE)
        return
    if not events:
        await max_client.send_message(max_user_id, EVENTS_EMPTY)
        return

    _EVENT_CACHE.clear()
    for event in events[:15]:
        _EVENT_CACHE[str(event["id"])] = event

    await max_client.send_message(
        max_user_id,
        "Выберите мероприятие для записи:",
        attachments=events_list_keyboard(events[:15]),
    )


async def try_handle_event_callback(
    session: AsyncSession,
    user: User,
    payload: str,
) -> bool:
    if payload == "menu_events":
        if not user.is_registered:
            await max_client.send_message(user.max_user_id, EVENTS_NEED_REGISTRATION)
            return True
        await send_events_list(user.max_user_id)
        return True

    if payload == "events_back":
        await send_events_list(user.max_user_id)
        return True

    if payload.startswith("event_pick:"):
        if not user.is_registered:
            await max_client.send_message(user.max_user_id, EVENTS_NEED_REGISTRATION)
            return True
        post_id = payload.split(":", 1)[1]
        event = _EVENT_CACHE.get(post_id)
        if not event:
            events = await fetch_open_events()
            if events:
                for e in events:
                    _EVENT_CACHE[str(e["id"])] = e
            event = _EVENT_CACHE.get(post_id)
        if not event:
            await max_client.send_message(user.max_user_id, "Мероприятие больше недоступно для записи.")
            await send_events_list(user.max_user_id)
            return True
        detail_text = _format_event_detail(event)
        image_url = event.get("imageUrl")
        if isinstance(image_url, str) and image_url.strip():
            await max_client.send_message_with_image(
                user.max_user_id,
                detail_text,
                image_url=image_url.strip(),
                attachments=event_detail_keyboard(post_id),
            )
        else:
            await max_client.send_message(
                user.max_user_id,
                detail_text,
                attachments=event_detail_keyboard(post_id),
            )
        return True

    if payload.startswith("event_register:"):
        if not user.is_registered:
            await max_client.send_message(user.max_user_id, EVENTS_NEED_REGISTRATION)
            return True
        post_id = payload.split(":", 1)[1]
        ok, message = await register_for_event(
            post_id,
            max_user_id=user.max_user_id,
            reporter_name=user.fio or user.max_name or "Житель",
            reporter_phone=user.phone,
            reporter_district=user.district,
        )
        await max_client.send_message(user.max_user_id, message)
        if ok:
            await send_main_menu(user.max_user_id, registered=True)
        return True

    return False
