from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import consent_keyboard, contact_keyboard, max_client
from app.models import RegistrationState, Screenshot, User
from app.texts import (
    ALREADY_REGISTERED,
    ASK_ADDRESS,
    ASK_FIO,
    ASK_PHONE,
    CONSENT_TEXT,
    INVALID_ADDRESS,
    INVALID_FIO,
    PHONE_DUPLICATE,
    PHONE_REQUIRED,
    REGISTRATION_COMPLETE,
    SCREENSHOT_NOT_REGISTERED,
    SCREENSHOT_RECEIVED,
    WELCOME_TEXT,
)


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


async def handle_callback(
    session: AsyncSession, update: dict[str, Any]
) -> None:
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


async def handle_message_created(
    session: AsyncSession, update: dict[str, Any]
) -> None:
    message = update.get("message") or {}
    body = message.get("body") or {}
    text = (body.get("text") or "").strip()
    attachments = body.get("attachments") or []

    user = await get_or_create_user(session, update)
    if not user:
        return

    if user.is_registered and _has_image_attachment(attachments):
        await _save_screenshot(session, user, message, attachments)
        return

    if user.state == RegistrationState.AWAITING_PHONE:
        contact = _extract_verified_contact(attachments)
        if contact:
            await _complete_phone_step(session, user, contact)
            return
        if text:
            await max_client.send_message(user.max_user_id, PHONE_REQUIRED, attachments=contact_keyboard())
        return

    if not text or user.is_registered:
        return

    if user.state == RegistrationState.AWAITING_FIO:
        if not is_valid_fio(text):
            await max_client.send_message(user.max_user_id, INVALID_FIO)
            return
        user.fio = text.strip()
        user.state = RegistrationState.AWAITING_ADDRESS
        await session.commit()
        await max_client.send_message(user.max_user_id, ASK_ADDRESS)
        return

    if user.state == RegistrationState.AWAITING_ADDRESS:
        if not is_valid_address(text):
            await max_client.send_message(user.max_user_id, INVALID_ADDRESS)
            return
        user.address = text.strip()
        user.state = RegistrationState.AWAITING_PHONE
        await session.commit()
        await max_client.send_message(user.max_user_id, ASK_PHONE, attachments=contact_keyboard())


async def _complete_phone_step(
    session: AsyncSession, user: User, contact: dict[str, Any]
) -> None:
    phone = normalize_phone(contact.get("phone", ""))
    if not phone:
        await max_client.send_message(user.max_user_id, PHONE_REQUIRED, attachments=contact_keyboard())
        return

    dup = await session.execute(
        select(User).where(User.phone == phone, User.id != user.id, User.is_registered.is_(True))
    )
    if dup.scalar_one_or_none():
        await max_client.send_message(user.max_user_id, PHONE_DUPLICATE)
        return

    user.phone = phone
    user.phone_verified = True
    user.state = RegistrationState.COMPLETED
    user.is_registered = True
    user.registered_at = datetime.now(timezone.utc)
    await session.commit()

    await max_client.send_message(
        user.max_user_id,
        REGISTRATION_COMPLETE.format(fio=user.fio or "гражданин"),
    )


def _extract_verified_contact(attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for attachment in attachments:
        if attachment.get("type") != "contact":
            continue
        payload = attachment.get("payload") or {}
        max_info = payload.get("max_info") or {}
        phone = max_info.get("phone") or payload.get("phone")
        if phone and payload.get("hash"):
            return {"phone": phone, "hash": payload.get("hash")}
    return None


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
