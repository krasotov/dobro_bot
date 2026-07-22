from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.max_client import consent_keyboard, contact_keyboard, max_client
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
    PHONE_DUPLICATE,
    PHONE_NOTE_UNVERIFIED,
    PHONE_NOTE_VERIFIED,
    PHONE_REQUIRED,
    REGISTRATION_COMPLETE,
    SCREENSHOT_NOT_REGISTERED,
    SCREENSHOT_RECEIVED,
    STATUS_REGISTERED,
    WELCOME_TEXT,
)
from app.services.appeal_flow import handle_appeal_flow_message, send_appeal_hint, try_start_appeal_flow
from app.utils.birth_date import parse_birth_date
from app.utils.contact import extract_contact_phone

logger = logging.getLogger(__name__)


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def is_valid_fio(value: str) -> bool:
    parts = [p for p in value.strip().split() if p]
    return len(parts) >= 2 and len(value.strip()) >= 5


def is_valid_address(value: str) -> bool:
    return len(value.strip()) >= 10


def is_valid_phone_text(value: str) -> bool:
    digits = normalize_phone(value)
    return len(digits) in {10, 11}


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
    user = await get_or_create_user(session, update)
    if not user:
        return

    callback_id = callback.get("callback_id")
    if callback_id:
        await max_client.answer_callback(callback_id, notification="Принято")

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

    if user.is_registered:
        if _has_image_attachment(attachments):
            await _save_screenshot(session, user, message, attachments)
            return
        if text:
            if await handle_appeal_flow_message(session, user, text):
                return
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
    if command in {"статус", "status"}:
        await max_client.send_message(user.max_user_id, STATUS_REGISTERED)
        return
    if command in {"обращение", "appeal", "заявка"}:
        await try_start_appeal_flow(session, user)
        return
    if command in {"помощь", "help"}:
        await max_client.send_message(user.max_user_id, HELP_REGISTERED)
        return
    await send_appeal_hint(user)


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
    await session.commit()

    phone_note = PHONE_NOTE_VERIFIED if verified else PHONE_NOTE_UNVERIFIED
    await max_client.send_message(
        user.max_user_id,
        REGISTRATION_COMPLETE.format(
            fio=user.fio or "гражданин",
            phone_note=phone_note,
        ),
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
        file_url=file_url,
        file_token=file_token,
        message_id=str(message.get("message_id") or message.get("id") or ""),
    )
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
