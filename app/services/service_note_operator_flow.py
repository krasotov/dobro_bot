from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.max_client import max_client, message_button, service_note_operator_cancel_keyboard
from app.models import ServiceNote, User
from app.services.audience_roles import user_is_it_operator
from app.services.service_note_service import (
    complete_service_note,
    format_service_note_summary,
    parse_note_attachments,
    take_service_note,
)
from app.texts import (
    SERVICE_NOTE_OPERATOR_ASK_COMMENT,
    SERVICE_NOTE_OPERATOR_CANCELLED,
    SERVICE_NOTE_OPERATOR_NEED_ROLE,
    SERVICE_NOTE_OPERATOR_NO_TICKETS,
)

OPERATOR_FLOW_NONE = "none"
OPERATOR_FLOW_AWAIT_COMMENT = "await_comment"


def in_service_note_operator_flow(user: User) -> bool:
    return (user.service_note_operator_flow_state or OPERATOR_FLOW_NONE) != OPERATOR_FLOW_NONE


def reset_service_note_operator_flow(user: User) -> None:
    user.service_note_operator_flow_state = OPERATOR_FLOW_NONE
    user.service_note_operator_ticket_id = None


STATUS_LABELS = {
    "new": "Новая",
    "in_progress": "В работе",
    "awaiting_user": "Ожидает приёмки",
    "reopened": "Возвращена",
    "archived": "Архив",
}

MSK = ZoneInfo("Europe/Moscow")
LIST_LIMIT = 10

_SN_VIEW = re.compile(r"^#(\d+)\s*$")
_SN_TAKE = re.compile(r"^взять\s+#?(\d+)\s*$", re.IGNORECASE)
_SN_DONE = re.compile(r"^готово\s+#?(\d+)\s*$", re.IGNORECASE)


def _note_datetime_local(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).astimezone(MSK)
    return dt.astimezone(MSK)


def _format_list_time(dt: datetime | None) -> str:
    local = _note_datetime_local(dt)
    if not local:
        return "—"
    return local.strftime("%d.%m %H:%M")


def _format_detail_time(dt: datetime | None) -> str:
    local = _note_datetime_local(dt)
    if not local:
        return "—"
    return local.strftime("%d.%m.%Y %H:%M")


def _short_fio(name: str | None) -> str:
    if not name or not str(name).strip():
        return "—"
    parts = str(name).strip().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return parts[0][:14]


def _status_prefix(status: str) -> str:
    if status == "new":
        return "🆕 "
    if status == "reopened":
        return "↩ "
    if status == "in_progress":
        return "▶ "
    return ""


def _note_list_button_label(note: ServiceNote, requester: User | None) -> str:
    who = _short_fio(requester.fio if requester else None)
    when = _format_list_time(note.created_at)
    room = (note.room_number or "—")[:12]
    label = f"{_status_prefix(note.status)}{when} · {who} · каб.{room}"
    return label[:64].strip()


def _format_notes_queue_summary(notes: list[ServiceNote]) -> str:
    lines = [
        "<b>Служебные записки (ИТ)</b>",
        f"В очереди: {len(notes)}",
        "",
        "Нажмите кнопку ниже, чтобы открыть заявку:",
        "",
    ]
    for index, note in enumerate(notes[:LIST_LIMIT], start=1):
        requester = note.requester
        status = STATUS_LABELS.get(note.status, note.status)
        when = _format_detail_time(note.created_at)
        who = requester.fio if requester and requester.fio else "—"
        room = note.room_number or "—"
        preview = (note.message or "").strip().replace("\n", " ")
        if len(preview) > 48:
            preview = f"{preview[:45]}…"
        attach_n = len(parse_note_attachments(note.attachments))
        attach_hint = f" · 📎{attach_n}" if attach_n else ""
        lines.append(f"{index}. <b>{status}</b> · {when} · {who} · каб. {room}{attach_hint}")
        if preview:
            lines.append(f"   <i>{preview}</i>")
    if len(notes) > LIST_LIMIT:
        lines.append(f"\n<i>Показаны первые {LIST_LIMIT} из {len(notes)}.</i>")
    return "\n".join(lines)


def _list_keyboard(notes: list[ServiceNote]) -> list[dict]:
    rows: list[list[dict]] = []
    shown = notes[:LIST_LIMIT]
    for index, note in enumerate(shown, start=1):
        label = _note_list_button_label(note, note.requester)
        if len(shown) > 1:
            label = f"{index}. {label}"[:64]
        rows.append(
            [{"type": "callback", "text": label, "payload": f"sn_op_view_{note.id}"}]
        )
    rows.append(
        [
            {"type": "callback", "text": "Обновить", "payload": "menu_service_notes_ops"},
            {"type": "callback", "text": "← Меню", "payload": "menu_open"},
        ]
    )
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def _detail_keyboard(note: ServiceNote, *, viewer: User) -> list[dict]:
    rows: list[list[dict]] = []
    if note.status in {"new", "reopened"}:
        rows.append(
            [{"type": "callback", "text": "Взять в работу", "payload": f"sn_op_take_{note.id}"}]
        )
    if note.status == "in_progress" and note.operator_id == viewer.id:
        rows.append(
            [{"type": "callback", "text": "Выполнено", "payload": f"sn_op_complete_{note.id}"}]
        )
    rows.append(
        [
            {"type": "callback", "text": "← К списку", "payload": "menu_service_notes_ops"},
            {"type": "callback", "text": "Меню", "payload": "menu_open"},
        ]
    )
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


async def send_operator_notes_list(
    session: AsyncSession,
    user: User,
    *,
    callback_id: str | None = None,
    chat_id: int | None = None,
) -> None:
    if not user_is_it_operator(user):
        await max_client.send_message(user.max_user_id, SERVICE_NOTE_OPERATOR_NEED_ROLE)
        return

    result = await session.execute(
        select(ServiceNote)
        .options(selectinload(ServiceNote.requester))
        .where(
            or_(
                ServiceNote.status.in_(("new", "reopened")),
                (ServiceNote.status == "in_progress") & (ServiceNote.operator_id == user.id),
            )
        )
        .order_by(ServiceNote.created_at.asc())
        .limit(20)
    )
    notes = list(result.scalars().all())

    if not notes:
        text = SERVICE_NOTE_OPERATOR_NO_TICKETS
        attachments = None
    else:
        text = _format_notes_queue_summary(notes)
        attachments = _list_keyboard(notes)

    await max_client.deliver_message(
        user.max_user_id,
        text,
        callback_id=callback_id,
        chat_id=chat_id,
        attachments=attachments,
        menu_bar=False,
    )


async def _show_note_detail(
    session: AsyncSession,
    user: User,
    note_id: int,
    *,
    chat_id: int | None = None,
) -> None:
    note = await session.get(
        ServiceNote,
        note_id,
        options=(selectinload(ServiceNote.requester),),
    )
    if not note:
        await max_client.send_message(user.max_user_id, "Заявка не найдена.")
        return

    status = STATUS_LABELS.get(note.status, note.status)
    summary = format_service_note_summary(note, requester=note.requester)
    text = f"{summary}\n\nСтатус: <b>{status}</b>"
    if note.resolution_comment:
        text += f"\n\n<b>Комментарий:</b> {note.resolution_comment}"

    metas = parse_note_attachments(note.attachments)
    if metas:
        text += f"\n\n<b>Вложений:</b> {len(metas)}"

    await max_client.send_message(
        user.max_user_id,
        text,
        chat_id=chat_id,
        attachments=_detail_keyboard(note, viewer=user),
        menu_bar=False,
    )

    if metas:
        from app.services.service_note_attachments import send_note_attachments_to_user

        sent = await send_note_attachments_to_user(user.max_user_id, note.attachments)
        if sent < len(metas):
            await max_client.send_message(
                user.max_user_id,
                "Часть вложений не удалось показать (часто у старых заявок ссылки MAX уже недействительны). "
                "Попросите заявителя отправить новую записку с фото или продублировать снимки в чат.",
                menu_bar=False,
            )


async def try_handle_service_note_operator_text(
    session: AsyncSession,
    user: User,
    text: str,
    *,
    chat_id: int | None = None,
) -> bool:
    if not user_is_it_operator(user):
        return False

    raw = text.strip()
    match = _SN_VIEW.match(raw)
    if match:
        note_id = int(match.group(1))
        reset_service_note_operator_flow(user)
        await session.commit()
        await _show_note_detail(session, user, note_id, chat_id=chat_id)
        return True

    match = _SN_TAKE.match(raw)
    if match:
        note_id = int(match.group(1))
        try:
            await take_service_note(session, note_id=note_id, operator=user)
            await max_client.send_message(
                user.max_user_id,
                f"Заявка #{note_id} взята в работу.",
                chat_id=chat_id,
            )
            await _show_note_detail(session, user, note_id, chat_id=chat_id)
        except ValueError as exc:
            await max_client.send_message(user.max_user_id, str(exc), chat_id=chat_id)
            await send_operator_notes_list(session, user, chat_id=chat_id)
        return True

    match = _SN_DONE.match(raw)
    if match:
        note_id = int(match.group(1))
        note = await session.get(ServiceNote, note_id)
        if not note or note.operator_id != user.id or note.status != "in_progress":
            await max_client.send_message(
                user.max_user_id,
                "Заявка недоступна для завершения.",
                chat_id=chat_id,
            )
            return True
        user.service_note_operator_flow_state = OPERATOR_FLOW_AWAIT_COMMENT
        user.service_note_operator_ticket_id = note_id
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            SERVICE_NOTE_OPERATOR_ASK_COMMENT,
            chat_id=chat_id,
            attachments=service_note_operator_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    return False


async def try_handle_service_note_operator_callback(
    session: AsyncSession,
    user: User,
    payload: str,
    *,
    callback_id: str | None = None,
    chat_id: int | None = None,
) -> bool:
    if payload == "menu_service_notes_ops":
        reset_service_note_operator_flow(user)
        await session.commit()
        await send_operator_notes_list(
            session,
            user,
            callback_id=callback_id,
            chat_id=chat_id,
        )
        return True

    if payload == "service_note_operator_cancel":
        if not in_service_note_operator_flow(user):
            return False
        reset_service_note_operator_flow(user)
        await session.commit()
        await max_client.send_message(user.max_user_id, SERVICE_NOTE_OPERATOR_CANCELLED)
        await send_operator_notes_list(session, user)
        return True

    if payload.startswith("sn_op_view_"):
        if not user_is_it_operator(user):
            await max_client.send_message(user.max_user_id, SERVICE_NOTE_OPERATOR_NEED_ROLE)
            return True
        note_id = int(payload.removeprefix("sn_op_view_"))
        reset_service_note_operator_flow(user)
        await session.commit()
        await _show_note_detail(session, user, note_id)
        return True

    if payload.startswith("sn_op_take_"):
        if not user_is_it_operator(user):
            await max_client.send_message(user.max_user_id, SERVICE_NOTE_OPERATOR_NEED_ROLE)
            return True
        note_id = int(payload.removeprefix("sn_op_take_"))
        try:
            await take_service_note(session, note_id=note_id, operator=user)
            await max_client.send_message(user.max_user_id, f"Заявка #{note_id} взята в работу.")
            await _show_note_detail(session, user, note_id)
        except ValueError as exc:
            await max_client.send_message(user.max_user_id, str(exc))
            await send_operator_notes_list(session, user)
        return True

    if payload.startswith("sn_op_complete_"):
        if not user_is_it_operator(user):
            await max_client.send_message(user.max_user_id, SERVICE_NOTE_OPERATOR_NEED_ROLE)
            return True
        note_id = int(payload.removeprefix("sn_op_complete_"))
        note = await session.get(ServiceNote, note_id)
        if not note or note.operator_id != user.id or note.status != "in_progress":
            await max_client.send_message(user.max_user_id, "Заявка недоступна для завершения.")
            return True
        user.service_note_operator_flow_state = OPERATOR_FLOW_AWAIT_COMMENT
        user.service_note_operator_ticket_id = note_id
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            SERVICE_NOTE_OPERATOR_ASK_COMMENT,
            attachments=service_note_operator_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    return False


async def handle_service_note_operator_message(session: AsyncSession, user: User, text: str) -> bool:
    if not in_service_note_operator_flow(user):
        return False

    if not user_is_it_operator(user):
        reset_service_note_operator_flow(user)
        await session.commit()
        return False

    normalized = text.strip()
    if normalized.lower() == "отмена":
        note_id = user.service_note_operator_ticket_id
        reset_service_note_operator_flow(user)
        await session.commit()
        await max_client.send_message(user.max_user_id, SERVICE_NOTE_OPERATOR_CANCELLED)
        if note_id:
            await _show_note_detail(session, user, note_id)
        else:
            await send_operator_notes_list(session, user)
        return True

    note_id = user.service_note_operator_ticket_id
    if not note_id:
        reset_service_note_operator_flow(user)
        await session.commit()
        return False

    comment = normalized if normalized and normalized != "—" else None
    try:
        await complete_service_note(
            session,
            note_id=note_id,
            operator=user,
            comment=comment,
        )
    except ValueError as exc:
        await max_client.send_message(user.max_user_id, str(exc))
        return True

    reset_service_note_operator_flow(user)
    await session.commit()
    await max_client.send_message(user.max_user_id, f"Заявка #{note_id} отмечена выполненной.")
    await send_operator_notes_list(session, user)
    return True
