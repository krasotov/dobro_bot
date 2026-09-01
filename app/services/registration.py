from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.max_client import consent_keyboard, contact_keyboard, main_menu_keyboard, max_client
from app.models import RegistrationState, Screenshot, User
from app.services.address import apply_address_to_user
from app.texts import (
    ALREADY_REGISTERED,
    ASK_ADDRESS,
    ASK_BIRTH_DATE,
    ASK_FIO,
    ASK_PHONE,
    ASK_PHONE_MANUAL,
    CONSENT_TEXT,
    CONTACT_PARSE_FAILED,
    HELP_REGISTERED,
    INVALID_ADDRESS,
    INVALID_BIRTH_DATE,
    INVALID_FIO,
    INVALID_PHONE,
    MENU_GUEST,
    PHONE_DUPLICATE,
    PHONE_NOTE_UNVERIFIED,
    PHONE_NOTE_VERIFIED,
    PHONE_REQUIRED,
    REGISTRATION_COMPLETE,
    SCREENSHOT_NOT_REGISTERED,
    SCREENSHOT_RECEIVED,
    WELCOME_TEXT,
)
from app.services.admspgo_directory import try_directory_command
from app.services.bot_menu import send_main_menu, try_handle_menu_callback
from app.services.event_flow import try_handle_event_callback
from app.services.poll_flow import try_handle_poll_callback
from app.services.topic_subscriptions import ensure_default_subscriptions, try_handle_topic_callback
from app.services.audience_roles import ensure_default_audience_roles
from app.services.profile_flow import (
    handle_profile_flow_message,
    in_profile_flow,
    send_profile_or_block,
    try_handle_profile_callback,
)
from app.services.appeal_flow import (
    APPEAL_CATEGORY_UTILITIES,
    APPEAL_FLOW_NONE,
    handle_appeal_flow_message,
    send_appeal_hint,
    try_handle_appeal_callback,
    try_start_appeal_flow,
)
from app.utils.birth_date import parse_birth_date
from app.utils.contact import extract_contact_phone
from app.utils.user_input import is_valid_address, is_valid_fio, is_valid_phone_text, normalize_phone

logger = logging.getLogger(__name__)


async def get_or_create_user(session: AsyncSession, update: dict[str, Any]) -> User | None:
    user_data = (
        update.get("user")
        or (update.get("callback") or {}).get("user")
        or (update.get("message") or {}).get("sender")
    )
    if not user_data:
        return None

    max_user_id = user_data.get("user_id")
    if not max_user_id:
        return None

    result = await session.execute(select(User).where(User.max_user_id == max_user_id))
    user = result.scalar_one_or_none()
    if user:
        user.max_username = user_data.get("username") or user.max_username
        user.max_name = user_data.get("name") or user.max_name
        return user

    user = User(
        max_user_id=max_user_id,
        max_username=user_data.get("username"),
        max_name=user_data.get("name"),
        state=RegistrationState.NEW,
    )
    session.add(user)
    await session.flush()
    return user


async def start_registration(session: AsyncSession, user: User) -> None:
    if user.is_registered:
        await max_client.send_message(user.max_user_id, ALREADY_REGISTERED)
        await send_main_menu(user.max_user_id, registered=True, user=user)
        return

    user.state = RegistrationState.AWAITING_CONSENT
    await session.commit()
    await max_client.send_message(
        user.max_user_id,
        f"{WELCOME_TEXT}\n\n{CONSENT_TEXT}",
        attachments=consent_keyboard(),
    )


async def handle_callback(session: AsyncSession, update: dict[str, Any]) -> None:
    callback = update.get("callback") or {}
    payload = callback.get("payload", "")
    callback_id = callback.get("callback_id")
    user = await get_or_create_user(session, update)
    if not user:
        return

    from app.services.callback_guard import is_debounced_action, is_duplicate_callback

    if callback_id and is_duplicate_callback(str(callback_id)):
        await max_client.answer_callback(str(callback_id), notification="Принято")
        return

    if callback_id and is_debounced_action(user.max_user_id, payload):
        await max_client.answer_callback(str(callback_id), notification="Принято")
        return

    if payload == "consent_yes":
        if user.is_registered:
            await max_client.send_message(user.max_user_id, ALREADY_REGISTERED)
            return
        user.consent_given = True
        user.consent_at = datetime.now(timezone.utc)
        user.state = RegistrationState.AWAITING_FIO
        await session.commit()
        await max_client.send_message(user.max_user_id, ASK_FIO)
        return

    if payload == "consent_no":
        user.state = RegistrationState.NEW
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            "Без согласия на обработку персональных данных регистрация невозможна. "
            "Если передумаете — нажмите «Начать» снова.",
        )
        return

    if payload == "phone_manual":
        if user.state not in {
            RegistrationState.AWAITING_PHONE,
            RegistrationState.AWAITING_PHONE_MANUAL,
        }:
            return
        user.state = RegistrationState.AWAITING_PHONE_MANUAL
        await session.commit()
        await max_client.send_message(user.max_user_id, ASK_PHONE_MANUAL)
        return

    if await try_handle_poll_callback(session, user, payload):
        return

    if await try_handle_event_callback(session, user, payload):
        return

    if await try_handle_topic_callback(session, user, payload):
        return

    if await try_handle_profile_callback(session, user, payload):
        return

    if await try_handle_appeal_callback(session, user, payload):
        return

    from app.services.transport_order_flow import try_handle_transport_callback
    from app.services.transport_operator_flow import try_handle_transport_operator_callback
    from app.services.appeal_operator_flow import try_handle_appeal_operator_callback

    if await try_handle_transport_callback(session, user, payload):
        return

    if await try_handle_transport_operator_callback(session, user, payload, callback_id=str(callback_id) if callback_id else None):
        return

    if await try_handle_appeal_operator_callback(session, user, payload, callback_id=str(callback_id) if callback_id else None):
        return

    if await try_handle_menu_callback(session, user, payload):
        return

    if callback_id:
        await max_client.answer_callback(str(callback_id), notification="Принято")


async def handle_message_created(session: AsyncSession, update: dict[str, Any]) -> None:
    message = update.get("message") or {}
    body = message.get("body") or {}
    text = (body.get("text") or "").strip()
    attachments = body.get("attachments") or []

    user = await get_or_create_user(session, update)
    if not user:
        logger.warning("message_created without user: %s", update)
        return

    logger.info(
        "message from %s state=%s text=%r attachments=%s",
        user.max_user_id,
        user.state,
        text[:50] if text else "",
        [a.get("type") for a in attachments],
    )

    if text and await try_directory_command(user.max_user_id, text):
        if user.is_registered:
            await send_main_menu(user.max_user_id, registered=True, user=user)
        else:
            await send_main_menu(user.max_user_id, registered=False)
        return

    if text:
        cmd = text.lower().strip()
        if cmd in {"меню", "menu"}:
            await send_main_menu(user.max_user_id, registered=user.is_registered, user=user)
            return
        if cmd in {"помощь", "help"} and not user.is_registered:
            await max_client.send_message(
                user.max_user_id,
                MENU_GUEST,
                attachments=main_menu_keyboard(registered=False),
            )
            return

    if user.is_registered:
        from app.services.appeal_operator_flow import handle_appeal_operator_message, in_appeal_operator_flow
        from app.services.transport_order_flow import handle_transport_flow_message, in_transport_flow

        in_appeal = (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_NONE
        if in_profile_flow(user) and (text or attachments):
            if await handle_profile_flow_message(session, user, text, attachments):
                return
        if in_appeal_operator_flow(user) and text:
            if await handle_appeal_operator_message(session, user, text):
                return
        if in_transport_flow(user) and text:
            if await handle_transport_flow_message(session, user, text):
                return
        if in_appeal or text or attachments:
            if await handle_appeal_flow_message(session, user, text, attachments):
                return
        if not in_appeal and not in_profile_flow(user) and _has_image_attachment(attachments):
            await _save_screenshot(session, user, message, attachments)
            return
        if text:
            await _handle_registered_text(session, user, text)
        return

    if user.state == RegistrationState.AWAITING_PHONE:
        contact = extract_contact_phone(attachments, bot_token=settings.max_bot_token)
        if contact:
            await _complete_phone_step(session, user, contact.get("phone", ""), verified=True)
            return

        has_contact_attachment = any(a.get("type") == "contact" for a in attachments)
        if has_contact_attachment:
            await max_client.send_message(
                user.max_user_id, CONTACT_PARSE_FAILED, attachments=contact_keyboard()
            )
            return

        if text and is_valid_phone_text(text):
            await _complete_phone_step(session, user, text, verified=False)
            return

        await max_client.send_message(user.max_user_id, PHONE_REQUIRED, attachments=contact_keyboard())
        return

    if user.state == RegistrationState.AWAITING_PHONE_MANUAL:
        if not text or not is_valid_phone_text(text):
            await max_client.send_message(user.max_user_id, INVALID_PHONE)
            return
        await _complete_phone_step(session, user, text, verified=False)
        return

    if user.state == RegistrationState.AWAITING_ADDRESS_CONFIRM:
        user.state = RegistrationState.AWAITING_PHONE
        await session.commit()
        await max_client.send_message(user.max_user_id, ASK_PHONE, attachments=contact_keyboard())
        return

    if not text:
        return

    if user.state == RegistrationState.AWAITING_FIO:
        if not is_valid_fio(text):
            await max_client.send_message(user.max_user_id, INVALID_FIO)
            return
        user.fio = text.strip()
        user.state = RegistrationState.AWAITING_BIRTH_DATE
        await session.commit()
        await max_client.send_message(user.max_user_id, ASK_BIRTH_DATE)
        return

    if user.state == RegistrationState.AWAITING_BIRTH_DATE:
        birth_date = parse_birth_date(text)
        if birth_date is None:
            await max_client.send_message(user.max_user_id, INVALID_BIRTH_DATE)
            return
        user.birth_date = birth_date
        user.state = RegistrationState.AWAITING_ADDRESS
        await session.commit()
        await max_client.send_message(user.max_user_id, ASK_ADDRESS)
        return

    if user.state == RegistrationState.AWAITING_ADDRESS:
        if not is_valid_address(text):
            await max_client.send_message(user.max_user_id, INVALID_ADDRESS)
            return
        await _process_address_input(session, user, text.strip())


async def _process_address_input(session: AsyncSession, user: User, raw_address: str) -> None:
    await apply_address_to_user(user, raw_address)
    user.state = RegistrationState.AWAITING_PHONE
    await session.commit()
    await max_client.send_message(user.max_user_id, ASK_PHONE, attachments=contact_keyboard())


async def _handle_registered_text(session: AsyncSession, user: User, text: str) -> None:
    command = text.lower().strip()
    if command in {"меню", "menu"}:
        await send_main_menu(user.max_user_id, registered=True, user=user)
        return
    if command in {"мероприятия", "мероприятие", "events", "event"}:
        from app.services.event_flow import send_events_list

        await send_events_list(user.max_user_id)
        return
    if command in {"опрос", "опросы", "poll", "polls"}:
        from app.services.poll_flow import send_polls_list

        await send_polls_list(user.max_user_id)
        return
    if command in {"подписки", "subscriptions", "topics"}:
        from app.services.topic_subscriptions import send_subscriptions_panel

        await send_subscriptions_panel(session, user)
        return
    if command in {"статус", "status", "профиль", "profile"}:
        in_appeal = (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_NONE
        await send_profile_or_block(session, user, in_appeal=in_appeal)
        return
    if command in {"обращение", "appeal", "заявка"}:
        await try_start_appeal_flow(session, user)
        return
    if command in {"жкх", "jkh", "utilities", "коммуналка"}:
        await try_start_appeal_flow(session, user, category=APPEAL_CATEGORY_UTILITIES)
        return
    if command in {"помощь", "help"}:
        await max_client.send_message(
            user.max_user_id,
            HELP_REGISTERED,
            attachments=main_menu_keyboard(registered=True),
        )
        return
    await send_appeal_hint(user)
    await send_main_menu(user.max_user_id, registered=True, user=user)


async def _complete_phone_step(
    session: AsyncSession,
    user: User,
    phone_raw: str,
    *,
    verified: bool,
) -> None:
    phone = normalize_phone(phone_raw)
    if not phone or len(phone) not in {10, 11}:
        await max_client.send_message(user.max_user_id, PHONE_REQUIRED, attachments=contact_keyboard())
        return

    dup = await session.execute(
        select(User).where(User.phone == phone, User.id != user.id, User.is_registered.is_(True))
    )
    if dup.scalar_one_or_none():
        await max_client.send_message(user.max_user_id, PHONE_DUPLICATE)
        return

    user.phone = phone
    user.phone_verified = verified
    user.state = RegistrationState.COMPLETED
    user.is_registered = True
    user.registered_at = datetime.now(timezone.utc)
    ensure_default_audience_roles(user)
    await ensure_default_subscriptions(user)
    await session.commit()

    phone_note = PHONE_NOTE_VERIFIED if verified else PHONE_NOTE_UNVERIFIED
    await max_client.send_message(
        user.max_user_id,
        REGISTRATION_COMPLETE.format(
            fio=user.fio or "гражданин",
            phone_note=phone_note,
        ),
        attachments=main_menu_keyboard(registered=True),
    )


def _has_image_attachment(attachments: list[dict[str, Any]]) -> bool:
    for attachment in attachments:
        if attachment.get("type") in {"image", "file", "photo"}:
            return True
    return False


async def _save_screenshot(
    session: AsyncSession,
    user: User,
    message: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> None:
    if not user.is_registered:
        await max_client.send_message(user.max_user_id, SCREENSHOT_NOT_REGISTERED)
        return

    file_url = None
    file_token = None
    for attachment in attachments:
        if attachment.get("type") in {"image", "file", "photo"}:
            payload = attachment.get("payload") or {}
            file_url = payload.get("url") or payload.get("file_url")
            file_token = payload.get("token")
            break

    screenshot = Screenshot(
        user_id=user.id,
        broadcast_id=user.pending_screenshot_broadcast_id,
        poll_id=user.pending_poll_id,
        file_url=file_url,
        file_token=file_token,
        message_id=str(message.get("message_id") or message.get("id") or ""),
    )
    user.pending_screenshot_broadcast_id = None
    user.pending_poll_id = None
    session.add(screenshot)
    await session.commit()
    await max_client.send_message(user.max_user_id, SCREENSHOT_RECEIVED)


async def handle_bot_started(session: AsyncSession, update: dict[str, Any]) -> None:
    user = await get_or_create_user(session, update)
    if not user:
        return
    await start_registration(session, user)


async def handle_bot_stopped(session: AsyncSession, update: dict[str, Any]) -> None:
    user = await get_or_create_user(session, update)
    if not user:
        return
    user.is_active = False
    await session.commit()
