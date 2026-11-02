from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.max_client import max_client, service_note_it_notify_keyboard
from app.models import ServiceNote, User
from app.services.audience_roles import user_is_it_operator

DEPARTMENT_LABELS = {"it": "ИТ отдел"}


def parse_note_attachments(raw: str | None) -> list[dict]:
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except json.JSONDecodeError:
        pass
    return []


def format_service_note_summary(note: ServiceNote, *, requester: User | None = None) -> str:
    dept = DEPARTMENT_LABELS.get(note.department, note.department)
    author = requester.fio if requester and requester.fio else "—"
    phone = requester.phone if requester and requester.phone else "—"
    return (
        f"<b>Заявка #{note.id}</b> · {dept}\n"
        f"Кабинет: <b>{note.room_number}</b>\n"
        f"Автор: {author}\n"
        f"Телефон: {phone}\n\n"
        f"{note.message}"
    )


async def notify_it_operators_new_note(session: AsyncSession, note: ServiceNote) -> None:
    requester = await session.get(User, note.requester_id)
    summary = format_service_note_summary(note, requester=requester)
    text = (
        f"🛠 <b>Новая служебная записка</b>\n\n{summary}\n\n"
        "Меню → «Заявки ИТ», чтобы взять в работу."
    )
    result = await session.execute(
        select(User).where(User.is_registered.is_(True), User.is_active.is_(True))
    )
    for user in result.scalars().all():
        if user_is_it_operator(user):
            try:
                await max_client.send_message(
                    user.max_user_id,
                    text,
                    attachments=service_note_it_notify_keyboard(),
                )
            except Exception:
                pass


async def take_service_note(session: AsyncSession, *, note_id: int, operator: User) -> ServiceNote:
    note = await session.get(
        ServiceNote,
        note_id,
        options=(selectinload(ServiceNote.requester),),
    )
    if not note or note.status not in {"new", "reopened"}:
        raise ValueError("Заявка недоступна")

    note.operator_id = operator.id
    note.status = "in_progress"
    note.assigned_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(note)

    requester = note.requester or await session.get(User, note.requester_id)
    if requester:
        operator_name = operator.fio or operator.max_name or "сотрудник ИТ"
        await max_client.send_message(
            requester.max_user_id,
            f"🛠 Заявку #{note.id} взял в работу: <b>{operator_name}</b>.",
        )
    from app.services.admspgo_service_notes import patch_service_note_on_admspgo

    await patch_service_note_on_admspgo(note, operator=operator)
    return note


async def complete_service_note(
    session: AsyncSession,
    *,
    note_id: int,
    operator: User,
    comment: str | None,
) -> ServiceNote:
    note = await session.get(
        ServiceNote,
        note_id,
        options=(selectinload(ServiceNote.requester),),
    )
    if not note or note.status != "in_progress" or note.operator_id != operator.id:
        raise ValueError("Заявка недоступна")

    note.resolution_comment = (comment or "").strip() or None
    note.resolved_at = datetime.now(timezone.utc)
    note.status = "awaiting_user"
    await session.commit()
    await session.refresh(note)

    requester = note.requester or await session.get(User, note.requester_id)
    if requester:
        lines = [f"✅ Заявка #{note.id} выполнена. Примите работу или сообщите, если проблема осталась."]
        if note.resolution_comment:
            lines.extend(["", f"<b>Комментарий ИТ:</b> {note.resolution_comment}"])
        from app.max_client import service_note_user_review_keyboard

        await max_client.send_message(
            requester.max_user_id,
            "\n".join(lines),
            attachments=service_note_user_review_keyboard(note.id),
        )
    from app.services.admspgo_service_notes import patch_service_note_on_admspgo

    await patch_service_note_on_admspgo(note, operator=operator)
    return note


async def accept_service_note(session: AsyncSession, *, note_id: int, requester: User) -> ServiceNote:
    note = await session.get(ServiceNote, note_id)
    if not note or note.requester_id != requester.id or note.status != "awaiting_user":
        raise ValueError("Заявка недоступна")

    note.status = "archived"
    note.archived_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(note)

    if note.operator_id:
        operator = await session.get(User, note.operator_id)
        if operator:
            await max_client.send_message(
                operator.max_user_id,
                f"✔️ Заявку #{note.id} принял заявитель. Архив.",
            )
    from app.services.admspgo_service_notes import patch_service_note_on_admspgo

    await patch_service_note_on_admspgo(note)
    return note


async def reject_service_note(session: AsyncSession, *, note_id: int, requester: User) -> ServiceNote:
    note = await session.get(ServiceNote, note_id)
    if not note or note.requester_id != requester.id or note.status != "awaiting_user":
        raise ValueError("Заявка недоступна")

    note.status = "reopened"
    note.resolved_at = None
    await session.commit()
    await session.refresh(note)

    if note.operator_id:
        operator = await session.get(User, note.operator_id)
        if operator:
            await max_client.send_message(
                operator.max_user_id,
                f"⚠️ Заявку #{note.id} не приняли — нужна доработка. Кабинет {note.room_number}.",
            )
    await max_client.send_message(
        requester.max_user_id,
        f"Заявка #{note.id} возвращена в работу ИТ. Ожидайте повторного выполнения.",
    )
    from app.services.admspgo_service_notes import patch_service_note_on_admspgo

    await patch_service_note_on_admspgo(note)
    return note
