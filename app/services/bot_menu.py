from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from app.max_client import (
    appeal_cancel_keyboard,
    appeal_operator_cancel_keyboard,
    main_menu_keyboard,
    max_client,
    profile_cancel_keyboard,
    service_note_cancel_keyboard,
    service_note_operator_cancel_keyboard,
    transport_cancel_keyboard,
)
from app.models import User
from app.services.admspgo_directory import send_directory_to_user
from app.texts import HELP_REGISTERED, MENU_GUEST, MENU_PROMPT

MENU_PAYLOADS = frozenset(
    {
        "menu_directory",
        "menu_appeal",
        "menu_help",
        "menu_profile",
        "menu_status",
        "menu_open",
        "menu_polls",
        "menu_transport",
        "menu_service_notes_ops",
    },
)


async def send_main_menu(
    max_user_id: int,
    *,
    registered: bool,
    user: User | None = None,
    text: str | None = None,
    callback_id: str | None = None,
    chat_id: int | None = None,
) -> None:
    if text is None and callback_id:
        stamp = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%H:%M")
        message = f"<b>Меню · {stamp}</b>\n\n{MENU_PROMPT}"
    else:
        message = text or MENU_PROMPT
    logger.info(
        "send_main_menu to max_user_id=%s chat_id=%s callback=%s",
        max_user_id,
        chat_id,
        bool(callback_id),
    )
    await max_client.deliver_message(
        max_user_id,
        message,
        callback_id=callback_id,
        chat_id=chat_id,
        attachments=main_menu_keyboard(registered=registered, user=user),
        menu_bar=False,
    )


async def try_handle_menu_callback(
    session: AsyncSession,
    user: User,
    payload: str,
    *,
    callback_id: str | None = None,
    chat_id: int | None = None,
) -> bool:
    if payload not in MENU_PAYLOADS:
        return False

    from app.services.appeal_flow import APPEAL_FLOW_NONE, try_start_appeal_flow
    from app.services.appeal_operator_flow import in_appeal_operator_flow
    from app.services.profile_flow import in_profile_flow, send_profile_or_block, try_handle_profile_callback as _profile_cb
    from app.services.service_note_flow import in_service_note_flow
    from app.services.service_note_operator_flow import in_service_note_operator_flow
    from app.services.transport_order_flow import in_transport_flow

    in_appeal = (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_NONE
    if in_appeal and payload not in {"menu_directory", "menu_open", "menu_events", "menu_polls", "menu_profile", "menu_status"}:
        await max_client.deliver_message(
            user.max_user_id,
            "Сначала завершите или отмените обращение.",
            callback_id=callback_id,
            attachments=appeal_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    if in_service_note_flow(user) and payload not in {"menu_directory", "menu_open"}:
        await max_client.deliver_message(
            user.max_user_id,
            "Сначала завершите или отмените служебную записку.",
            callback_id=callback_id,
            attachments=service_note_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    if in_service_note_operator_flow(user) and payload not in {
        "menu_directory",
        "menu_open",
        "menu_service_notes_ops",
    }:
        await max_client.deliver_message(
            user.max_user_id,
            "Сначала завершите или отмените ввод комментария по заявке.",
            callback_id=callback_id,
            attachments=service_note_operator_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    if in_transport_flow(user) and payload not in {"menu_directory", "menu_open"}:
        await max_client.deliver_message(
            user.max_user_id,
            "Сначала завершите или отмените заявку на автомобиль.",
            callback_id=callback_id,
            attachments=transport_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    if in_profile_flow(user) and payload not in {"menu_directory", "menu_open", "menu_profile", "menu_status"}:
        await max_client.deliver_message(
            user.max_user_id,
            "Сначала завершите или отмените редактирование профиля.",
            callback_id=callback_id,
            attachments=profile_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    if in_appeal_operator_flow(user) and payload not in {"menu_directory", "menu_open", "menu_appeals_ops"}:
        await max_client.deliver_message(
            user.max_user_id,
            "Сначала завершите или отмените ввод ответа заявителю.",
            callback_id=callback_id,
            attachments=appeal_operator_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    if payload == "menu_directory":
        await send_directory_to_user(user.max_user_id)
        await send_main_menu(user.max_user_id, registered=user.is_registered, user=user)
        return True

    if not user.is_registered:
        await max_client.send_message(user.max_user_id, MENU_GUEST)
        await send_main_menu(user.max_user_id, registered=False)
        return True

    if payload == "menu_transport":
        from app.services.transport_order_flow import try_start_transport_order

        await try_start_transport_order(session, user)
        return True

    if payload == "menu_appeal":
        await try_start_appeal_flow(session, user)
        return True

    if payload == "menu_service_notes_ops":
        from app.services.service_note_operator_flow import send_operator_notes_list

        await send_operator_notes_list(
            session,
            user,
            callback_id=callback_id,
            chat_id=chat_id,
        )
        return True

    if payload == "menu_help":
        await max_client.send_message(
            user.max_user_id,
            HELP_REGISTERED,
            attachments=main_menu_keyboard(registered=True),
        )
        return True

    if payload in {"menu_profile", "menu_status"}:
        await _profile_cb(session, user, payload)
        return True

    if payload == "menu_open":
        await send_main_menu(
            user.max_user_id,
            registered=user.is_registered,
            user=user,
            callback_id=callback_id,
            chat_id=chat_id,
        )
        return True

    return False
