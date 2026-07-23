from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import max_client
from app.models import User
from app.services.admspgo_appeals import download_attachment, submit_max_appeal
from app.services.appeal_attachments import (
    MAX_APPEAL_ATTACHMENTS,
    extract_attachment_meta,
    parse_draft_attachments,
    serialize_draft_attachments,
)
from app.texts import (
    APPEAL_ASK_ATTACHMENTS,
    APPEAL_ASK_MESSAGE,
    APPEAL_ASK_SUBJECT,
    APPEAL_ATTACHMENT_LIMIT,
    APPEAL_ATTACHMENT_SAVED,
    APPEAL_CANCELLED,
    APPEAL_INVALID_MESSAGE,
    APPEAL_INVALID_SUBJECT,
    APPEAL_NEED_REGISTRATION,
    APPEAL_SENT,
    APPEAL_START_HINT,
)

APPEAL_FLOW_NONE = "none"
APPEAL_FLOW_SUBJECT = "subject"
APPEAL_FLOW_MESSAGE = "message"
APPEAL_FLOW_ATTACH = "attach"

SUBMIT_WORDS = {"отправить", "готово", "send", "submit"}


def _address_full(user: User) -> str | None:
    text = (user.address_normalized or user.address or "").strip()
    return text or None


def _clear_draft(user: User) -> None:
    user.appeal_flow_state = APPEAL_FLOW_NONE
    user.appeal_draft_subject = None
    user.appeal_draft_message = None
    user.appeal_draft_attachments = None


async def try_start_appeal_flow(session: AsyncSession, user: User) -> bool:
    if not user.is_registered:
        await max_client.send_message(user.max_user_id, APPEAL_NEED_REGISTRATION)
        return True

    if user.appeal_flow_state and user.appeal_flow_state != APPEAL_FLOW_NONE:
        await max_client.send_message(
            user.max_user_id,
            "Вы уже заполняете обращение. Введите текст или напишите <b>отмена</b>.",
        )
        return True

    user.appeal_flow_state = APPEAL_FLOW_SUBJECT
    user.appeal_draft_subject = None
    user.appeal_draft_message = None
    user.appeal_draft_attachments = None
    await session.commit()
    await max_client.send_message(user.max_user_id, APPEAL_ASK_SUBJECT)
    return True


async def cancel_appeal_flow(session: AsyncSession, user: User) -> None:
    _clear_draft(user)
    await session.commit()
    await max_client.send_message(user.max_user_id, APPEAL_CANCELLED)


async def _submit_appeal(session: AsyncSession, user: User) -> None:
    address = _address_full(user)
    if not address:
        _clear_draft(user)
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            "В профиле нет адреса — обратитесь в администрацию для обновления данных.",
        )
        return

    subject = (user.appeal_draft_subject or "Обращение из MAX").strip()
    message = (user.appeal_draft_message or "").strip()
    if len(message) < 10:
        await max_client.send_message(user.max_user_id, APPEAL_INVALID_MESSAGE)
        return

    payload = {
        "maxUserId": user.max_user_id,
        "subject": subject,
        "message": message[:8000],
        "reporterName": user.fio or "Житель",
        "reporterPhone": user.phone,
        "reporterDistrict": user.district,
        "addressFull": address,
        "addressLabel": "MAX",
    }

    metas = parse_draft_attachments(user.appeal_draft_attachments)
    files: list[tuple[str, bytes, str]] = []
    for meta in metas:
        downloaded = await download_attachment(meta)
        if downloaded:
            files.append(downloaded)

    ok, err = await submit_max_appeal(payload, files if files else None)

    _clear_draft(user)
    await session.commit()

    if ok:
        await max_client.send_message(user.max_user_id, APPEAL_SENT)
    else:
        await max_client.send_message(user.max_user_id, err or APPEAL_START_HINT)


async def handle_appeal_flow_message(
    session: AsyncSession,
    user: User,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    state = (user.appeal_flow_state or APPEAL_FLOW_NONE).lower()
    if state == APPEAL_FLOW_NONE:
        return False

    attachments = attachments or []
    lowered = text.lower().strip()
    if lowered in {"отмена", "cancel"}:
        await cancel_appeal_flow(session, user)
        return True

    if state == APPEAL_FLOW_ATTACH:
        meta = extract_attachment_meta(attachments)
        if meta:
            items = parse_draft_attachments(user.appeal_draft_attachments)
            if len(items) >= MAX_APPEAL_ATTACHMENTS:
                await max_client.send_message(user.max_user_id, APPEAL_ATTACHMENT_LIMIT)
                return True
            items.append(meta)
            user.appeal_draft_attachments = serialize_draft_attachments(items)
            await session.commit()
            await max_client.send_message(
                user.max_user_id,
                APPEAL_ATTACHMENT_SAVED.format(count=len(items)),
            )
            return True

        if lowered in SUBMIT_WORDS or (not text and not attachments):
            await _submit_appeal(session, user)
            return True

        if text:
            await max_client.send_message(
                user.max_user_id,
                "Приложите фото через 📎 или напишите <b>отправить</b>.",
            )
            return True
        return True

    if state == APPEAL_FLOW_SUBJECT:
        subject = text.strip()
        if len(subject) < 3:
            await max_client.send_message(user.max_user_id, APPEAL_INVALID_SUBJECT)
            return True
        user.appeal_draft_subject = subject[:200]
        user.appeal_flow_state = APPEAL_FLOW_MESSAGE
        await session.commit()
        await max_client.send_message(user.max_user_id, APPEAL_ASK_MESSAGE)
        return True

    if state == APPEAL_FLOW_MESSAGE:
        message = text.strip()
        if len(message) < 10:
            await max_client.send_message(user.max_user_id, APPEAL_INVALID_MESSAGE)
            return True

        user.appeal_draft_message = message[:8000]
        user.appeal_flow_state = APPEAL_FLOW_ATTACH
        user.appeal_draft_attachments = None
        await session.commit()
        await max_client.send_message(user.max_user_id, APPEAL_ASK_ATTACHMENTS)
        return True

    _clear_draft(user)
    await session.commit()
    return False


async def send_appeal_hint(user: User) -> None:
    await max_client.send_message(user.max_user_id, APPEAL_START_HINT)
