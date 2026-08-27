from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.max_client import contact_keyboard, main_menu_keyboard, max_client, profile_edit_keyboard
from app.models import User
from app.services.address import apply_address_to_user
from app.services.bot_menu import send_main_menu
from app.utils.user_input import is_valid_address, is_valid_fio, is_valid_phone_text, normalize_phone
from app.texts import (
    ASK_ADDRESS,
    ASK_BIRTH_DATE,
    ASK_FIO,
    ASK_PHONE,
    ASK_PHONE_MANUAL,
    CONTACT_PARSE_FAILED,
    INVALID_ADDRESS,
    INVALID_BIRTH_DATE,
    INVALID_FIO,
    INVALID_PHONE,
    PHONE_DUPLICATE,
    PHONE_REQUIRED,
    PROFILE_CANCELLED,
    PROFILE_IN_APPEAL,
    PROFILE_SAVED,
    PROFILE_ASK_FIO,
    PROFILE_ASK_BIRTH,
    PROFILE_ASK_ADDRESS,
)
from app.utils.birth_date import parse_birth_date
from app.utils.contact import extract_contact_phone

PROFILE_FLOW_NONE = "none"
PROFILE_FLOW_FIO = "fio"
PROFILE_FLOW_BIRTH = "birth"
PROFILE_FLOW_ADDRESS = "address"
PROFILE_FLOW_PHONE = "phone"
PROFILE_FLOW_PHONE_MANUAL = "phone_manual"


def in_profile_flow(user: User) -> bool:
    return (user.profile_flow_state or PROFILE_FLOW_NONE) != PROFILE_FLOW_NONE


def _clear_profile_flow(user: User) -> None:
    user.profile_flow_state = PROFILE_FLOW_NONE


def _display_address(user: User) -> str:
    return (user.address_normalized or user.address or "—").strip() or "—"


def _display_phone(user: User) -> str:
    phone = (user.phone or "").strip()
    if not phone:
        return "—"
    verified = " (подтверждён в MAX)" if user.phone_verified else " (введён вручную)"
    return f"+{phone.lstrip('+')}{verified}"


def format_profile_text(user: User) -> str:
    district = (user.district or "").strip() or "—"
    return (
        "<b>Ваш профиль</b>\n\n"
        f"<b>ФИО:</b> {user.fio or '—'}\n"
        f"<b>Дата рождения:</b> {user.birth_date or '—'}\n"
        f"<b>Адрес:</b> {_display_address(user)}\n"
        f"<b>Район:</b> {district}\n"
        f"<b>Телефон:</b> {_display_phone(user)}\n\n"
        "Нажмите кнопку ниже, чтобы изменить поле."
    )


async def send_profile_panel(user: User) -> None:
    await max_client.send_message(
        user.max_user_id,
        format_profile_text(user),
        attachments=profile_edit_keyboard(),
    )


async def send_profile_or_block(session: AsyncSession, user: User, *, in_appeal: bool) -> None:
    if in_appeal:
        await max_client.send_message(user.max_user_id, PROFILE_IN_APPEAL)
        return
    _clear_profile_flow(user)
    await session.commit()
    await send_profile_panel(user)


async def try_handle_profile_callback(session: AsyncSession, user: User, payload: str) -> bool:
    if not user.is_registered:
        return False

    if payload == "menu_profile" or payload == "menu_status":
        from app.services.appeal_flow import APPEAL_FLOW_NONE

        in_appeal = (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_NONE
        await send_profile_or_block(session, user, in_appeal=in_appeal)
        return True

    if payload == "profile_back":
        if not in_profile_flow(user):
            await send_profile_panel(user)
            return True
        _clear_profile_flow(user)
        await session.commit()
        await send_profile_panel(user)
        return True

    if payload == "profile_cancel":
        if not in_profile_flow(user):
            return False
        _clear_profile_flow(user)
        await session.commit()
        await max_client.send_message(user.max_user_id, PROFILE_CANCELLED)
        await send_main_menu(user.max_user_id, registered=True)
        return True

    if payload == "profile_phone_manual":
        if (user.profile_flow_state or PROFILE_FLOW_NONE) != PROFILE_FLOW_PHONE:
            return False
        user.profile_flow_state = PROFILE_FLOW_PHONE_MANUAL
        await session.commit()
        await max_client.send_message(user.max_user_id, ASK_PHONE_MANUAL)
        return True

    edit_map = {
        "profile_edit_fio": (PROFILE_FLOW_FIO, PROFILE_ASK_FIO),
        "profile_edit_birth": (PROFILE_FLOW_BIRTH, PROFILE_ASK_BIRTH),
        "profile_edit_address": (PROFILE_FLOW_ADDRESS, PROFILE_ASK_ADDRESS),
        "profile_edit_phone": (PROFILE_FLOW_PHONE, ASK_PHONE),
    }
    if payload in edit_map:
        from app.services.appeal_flow import APPEAL_FLOW_NONE

        if (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_NONE:
            await max_client.send_message(user.max_user_id, PROFILE_IN_APPEAL)
            return True
        state, prompt = edit_map[payload]
        user.profile_flow_state = state
        await session.commit()
        attachments = contact_keyboard() if state == PROFILE_FLOW_PHONE else None
        await max_client.send_message(user.max_user_id, prompt, attachments=attachments)
        return True

    return False


async def _save_phone(session: AsyncSession, user: User, phone_raw: str, *, verified: bool) -> bool:
    phone = normalize_phone(phone_raw)
    if not phone or len(phone) not in {10, 11}:
        await max_client.send_message(user.max_user_id, INVALID_PHONE)
        return False

    dup = await session.execute(
        select(User).where(User.phone == phone, User.id != user.id, User.is_registered.is_(True))
    )
    if dup.scalar_one_or_none():
        await max_client.send_message(user.max_user_id, PHONE_DUPLICATE)
        return False

    user.phone = phone
    user.phone_verified = verified
    return True


async def handle_profile_flow_message(
    session: AsyncSession,
    user: User,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    state = (user.profile_flow_state or PROFILE_FLOW_NONE).lower()
    if state == PROFILE_FLOW_NONE:
        return False

    attachments = attachments or []
    lowered = text.lower().strip()
    if lowered in {"отмена", "cancel"}:
        _clear_profile_flow(user)
        await session.commit()
        await max_client.send_message(user.max_user_id, PROFILE_CANCELLED)
        await send_profile_panel(user)
        return True

    if state == PROFILE_FLOW_PHONE:
        contact = extract_contact_phone(attachments, bot_token=settings.max_bot_token)
        if contact:
            if await _save_phone(session, user, contact.get("phone", ""), verified=True):
                _clear_profile_flow(user)
                await session.commit()
                await max_client.send_message(user.max_user_id, PROFILE_SAVED)
                await send_profile_panel(user)
            return True
        if any(a.get("type") == "contact" for a in attachments):
            await max_client.send_message(
                user.max_user_id, CONTACT_PARSE_FAILED, attachments=contact_keyboard()
            )
            return True
        if text:
            await max_client.send_message(user.max_user_id, PHONE_REQUIRED, attachments=contact_keyboard())
        return True

    if state == PROFILE_FLOW_PHONE_MANUAL:
        if not text or not is_valid_phone_text(text):
            await max_client.send_message(user.max_user_id, INVALID_PHONE)
            return True
        if await _save_phone(session, user, text, verified=False):
            _clear_profile_flow(user)
            await session.commit()
            await max_client.send_message(user.max_user_id, PROFILE_SAVED)
            await send_profile_panel(user)
        return True

    if not text:
        return True

    if state == PROFILE_FLOW_FIO:
        if not is_valid_fio(text):
            await max_client.send_message(user.max_user_id, INVALID_FIO)
            return True
        user.fio = text.strip()
        _clear_profile_flow(user)
        await session.commit()
        await max_client.send_message(user.max_user_id, PROFILE_SAVED)
        await send_profile_panel(user)
        return True

    if state == PROFILE_FLOW_BIRTH:
        birth_date = parse_birth_date(text)
        if birth_date is None:
            await max_client.send_message(user.max_user_id, INVALID_BIRTH_DATE)
            return True
        user.birth_date = birth_date
        _clear_profile_flow(user)
        await session.commit()
        await max_client.send_message(user.max_user_id, PROFILE_SAVED)
        await send_profile_panel(user)
        return True

    if state == PROFILE_FLOW_ADDRESS:
        if not is_valid_address(text):
            await max_client.send_message(user.max_user_id, INVALID_ADDRESS)
            return True
        await apply_address_to_user(user, text.strip())
        _clear_profile_flow(user)
        await session.commit()
        await max_client.send_message(user.max_user_id, PROFILE_SAVED)
        await send_profile_panel(user)
        return True

    _clear_profile_flow(user)
    await session.commit()
    return False
