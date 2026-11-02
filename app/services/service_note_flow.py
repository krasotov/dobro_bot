from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import max_client, service_note_attach_keyboard, service_note_cancel_keyboard
from app.models import ServiceNote, User
from app.services.appeal_attachments import (
    MAX_APPEAL_ATTACHMENTS,
    extract_attachment_meta,
    parse_draft_attachments,
    serialize_draft_attachments,
)
from app.services.audience_roles import user_can_create_service_note
from app.services.bot_menu import send_main_menu
from app.services.service_note_service import notify_it_operators_new_note

FLOW_NONE = "none"
FLOW_DEPARTMENT = "department"
FLOW_ROOM = "room"
FLOW_MESSAGE = "message"
FLOW_ATTACH = "attach"

SUBMIT_WORDS = {"отправить", "готово", "send", "submit"}


def in_service_note_flow(user: User) -> bool:
    return (user.service_note_flow_state or FLOW_NONE) != FLOW_NONE


def _clear_draft(user: User) -> None:
    user.service_note_flow_state = FLOW_NONE
    user.service_note_draft_department = None
    user.service_note_draft_room = None
    user.service_note_draft_message = None
    user.service_note_draft_attachments = None


def department_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [{"type": "callback", "text": "ИТ отдел", "payload": "service_note_dept_it"}],
                    [{"type": "callback", "text": "Отмена", "payload": "service_note_cancel"}],
                ],
            },
        }
    ]


async def try_start_service_note_flow(session: AsyncSession, user: User) -> bool:
    from app.texts import SERVICE_NOTE_NEED_ROLE, SERVICE_NOTE_START

    if not user.is_registered:
        from app.texts import APPEAL_NEED_REGISTRATION

        await max_client.send_message(user.max_user_id, APPEAL_NEED_REGISTRATION)
        return True

    if not user_can_create_service_note(user):
        await max_client.send_message(user.max_user_id, SERVICE_NOTE_NEED_ROLE)
        return True

    from app.services.appeal_flow import APPEAL_FLOW_NONE

    if (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_NONE:
        from app.max_client import appeal_cancel_keyboard

        await max_client.send_message(
            user.max_user_id,
            "Сначала завершите или отмените обращение.",
            attachments=appeal_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    if in_service_note_flow(user):
        await max_client.send_message(
            user.max_user_id,
            "Вы уже заполняете служебную записку. Продолжите ввод или отмените.",
            attachments=service_note_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    user.appeal_flow_state = APPEAL_FLOW_NONE
    user.service_note_flow_state = FLOW_DEPARTMENT
    user.service_note_draft_department = None
    user.service_note_draft_room = None
    user.service_note_draft_message = None
    user.service_note_draft_attachments = None
    await session.commit()

    await max_client.send_message(
        user.max_user_id,
        SERVICE_NOTE_START,
        attachments=department_keyboard(),
        menu_bar=False,
    )
    return True


async def try_handle_service_note_callback(session: AsyncSession, user: User, payload: str) -> bool:
    if payload == "service_note_dept_it":
        if (user.service_note_flow_state or FLOW_NONE) != FLOW_DEPARTMENT:
            return False
        if not user_can_create_service_note(user):
            return False
        user.service_note_draft_department = "it"
        user.service_note_flow_state = FLOW_ROOM
        await session.commit()
        from app.texts import SERVICE_NOTE_ASK_ROOM

        await max_client.send_message(
            user.max_user_id,
            SERVICE_NOTE_ASK_ROOM,
            attachments=service_note_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    if payload == "service_note_cancel":
        if not in_service_note_flow(user):
            return False
        await cancel_service_note_flow(session, user)
        return True

    if payload == "service_note_submit":
        if (user.service_note_flow_state or FLOW_NONE) != FLOW_ATTACH:
            return False
        await _submit_service_note(session, user)
        return True

    if payload.startswith("sn_user_accept_"):
        from app.services.service_note_service import accept_service_note

        note_id = int(payload.removeprefix("sn_user_accept_"))
        try:
            await accept_service_note(session, note_id=note_id, requester=user)
            await max_client.send_message(user.max_user_id, f"Спасибо! Заявка #{note_id} закрыта.")
            await send_main_menu(user.max_user_id, registered=True, user=user)
        except ValueError as exc:
            await max_client.send_message(user.max_user_id, str(exc))
        return True

    if payload.startswith("sn_user_reject_"):
        from app.services.service_note_service import reject_service_note

        note_id = int(payload.removeprefix("sn_user_reject_"))
        try:
            await reject_service_note(session, note_id=note_id, requester=user)
        except ValueError as exc:
            await max_client.send_message(user.max_user_id, str(exc))
        return True

    return False


async def cancel_service_note_flow(session: AsyncSession, user: User) -> None:
    from app.texts import SERVICE_NOTE_CANCELLED

    _clear_draft(user)
    await session.commit()
    await max_client.send_message(user.max_user_id, SERVICE_NOTE_CANCELLED)
    await send_main_menu(user.max_user_id, registered=True, user=user)


async def _submit_service_note(session: AsyncSession, user: User) -> None:
    from app.texts import SERVICE_NOTE_INVALID_MESSAGE, SERVICE_NOTE_SENT

    room = (user.service_note_draft_room or "").strip()
    message = (user.service_note_draft_message or "").strip()
    dept = (user.service_note_draft_department or "it").strip().lower()

    if len(room) < 1 or len(room) > 64:
        await max_client.send_message(
            user.max_user_id,
            "Укажите номер кабинета.",
            attachments=service_note_cancel_keyboard(),
            menu_bar=False,
        )
        return

    if len(message) < 5:
        await max_client.send_message(
            user.max_user_id,
            SERVICE_NOTE_INVALID_MESSAGE,
            attachments=service_note_cancel_keyboard(),
            menu_bar=False,
        )
        return

    from app.services.service_note_attachments import materialize_service_note_attachments

    draft_had_attachments = bool(parse_draft_attachments(user.service_note_draft_attachments))
    persisted_attachments = await materialize_service_note_attachments(user.service_note_draft_attachments)
    lost_attachments = draft_had_attachments and not persisted_attachments

    note = ServiceNote(
        requester_id=user.id,
        department=dept,
        room_number=room[:64],
        message=message[:8000],
        attachments=persisted_attachments,
        status="new",
    )
    session.add(note)
    _clear_draft(user)
    await session.commit()
    await session.refresh(note)

    sent_text = SERVICE_NOTE_SENT.format(note_id=note.id)
    if lost_attachments:
        sent_text += (
            "\n\n⚠️ Фото не удалось прикрепить к заявке — отправьте снимки ещё раз отдельным сообщением "
            "или создайте записку заново."
        )
    await max_client.send_message(user.max_user_id, sent_text)
    await notify_it_operators_new_note(session, note)
    from app.services.admspgo_service_notes import push_service_note_to_admspgo

    await push_service_note_to_admspgo(note, requester=user)
    await send_main_menu(user.max_user_id, registered=True, user=user)


async def handle_service_note_flow_message(
    session: AsyncSession,
    user: User,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    from app.texts import (
        APPEAL_ASK_ATTACHMENTS,
        APPEAL_ATTACHMENT_LIMIT,
        APPEAL_ATTACHMENT_SAVED,
        SERVICE_NOTE_ASK_MESSAGE,
        SERVICE_NOTE_INVALID_MESSAGE,
    )

    state = (user.service_note_flow_state or FLOW_NONE).lower()
    if state == FLOW_NONE:
        return False

    attachments = attachments or []
    lowered = text.lower().strip()
    if lowered in {"отмена", "cancel"}:
        await cancel_service_note_flow(session, user)
        return True

    if state == FLOW_DEPARTMENT:
        await max_client.send_message(
            user.max_user_id,
            "Выберите отдел кнопкой ниже.",
            attachments=department_keyboard(),
            menu_bar=False,
        )
        return True

    if state == FLOW_ROOM:
        room = text.strip()
        if not room or len(room) > 64:
            from app.texts import SERVICE_NOTE_ASK_ROOM

            await max_client.send_message(
                user.max_user_id,
                SERVICE_NOTE_ASK_ROOM,
                attachments=service_note_cancel_keyboard(),
                menu_bar=False,
            )
            return True
        user.service_note_draft_room = room
        user.service_note_flow_state = FLOW_MESSAGE
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            SERVICE_NOTE_ASK_MESSAGE,
            attachments=service_note_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    if state == FLOW_MESSAGE:
        message = text.strip()
        if len(message) < 5:
            await max_client.send_message(
                user.max_user_id,
                SERVICE_NOTE_INVALID_MESSAGE,
                attachments=service_note_cancel_keyboard(),
                menu_bar=False,
            )
            return True
        user.service_note_draft_message = message[:8000]
        user.service_note_flow_state = FLOW_ATTACH
        user.service_note_draft_attachments = None
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            APPEAL_ASK_ATTACHMENTS,
            attachments=service_note_attach_keyboard(),
            menu_bar=False,
        )
        return True

    if state == FLOW_ATTACH:
        meta = extract_attachment_meta(attachments)
        if meta:
            items = parse_draft_attachments(user.service_note_draft_attachments)
            if len(items) >= MAX_APPEAL_ATTACHMENTS:
                await max_client.send_message(
                    user.max_user_id,
                    APPEAL_ATTACHMENT_LIMIT,
                    attachments=service_note_attach_keyboard(),
                    menu_bar=False,
                )
                return True
            items.append(meta)
            user.service_note_draft_attachments = serialize_draft_attachments(items)
            await session.commit()
            await max_client.send_message(
                user.max_user_id,
                APPEAL_ATTACHMENT_SAVED.format(count=len(items)),
                attachments=service_note_attach_keyboard(),
                menu_bar=False,
            )
            return True

        if lowered in SUBMIT_WORDS:
            await _submit_service_note(session, user)
            return True

        if text:
            await max_client.send_message(
                user.max_user_id,
                "Приложите фото через 📎 или нажмите <b>Отправить</b>.",
                attachments=service_note_attach_keyboard(),
                menu_bar=False,
            )
            return True
        return True

    _clear_draft(user)
    await session.commit()
    return False
