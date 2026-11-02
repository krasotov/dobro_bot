from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import httpx

from app.config import settings
from app.max_client import max_client, service_note_user_review_keyboard
from app.models import ServiceNote, User
from app.services.appeal_attachments import guess_mime, _looks_like_image


async def _send_delivery_files(max_user_id: int, files: list[dict[str, str]]) -> None:
    if not files:
        return
    headers: dict[str, str] = {}
    token = (settings.admspgo_internal_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for item in files[:5]:
            url = (item.get("url") or "").strip()
            name = (item.get("fileName") or "file").strip()[:200]
            if not url:
                continue
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.content
            except Exception:
                continue
            if len(data) > 12 * 1024 * 1024:
                continue
            mime = guess_mime(name)
            try:
                if _looks_like_image(name, mime):
                    token_max = await max_client.upload_image(data, name)
                    att_type = "image"
                else:
                    token_max = await max_client.upload_file(data, name)
                    att_type = "file"
                await max_client.send_message(
                    max_user_id,
                    f"📎 {name}",
                    attachments=[{"type": att_type, "payload": {"token": token_max}}],
                    menu_bar=False,
                )
            except Exception:
                continue


async def apply_service_note_external_sync(
    session: AsyncSession,
    *,
    note_id: int,
    status: str,
    resolution_comment: str | None,
    operator_fio: str | None,
    delivery_files: list[dict[str, str]] | None = None,
) -> ServiceNote:
    note = await session.get(
        ServiceNote,
        note_id,
        options=(selectinload(ServiceNote.requester),),
    )
    if not note:
        raise ValueError("Заявка не найдена")

    normalized = (status or "").strip().lower()
    allowed = {"new", "in_progress", "awaiting_user", "reopened", "archived"}
    if normalized not in allowed:
        raise ValueError("Некорректный статус")

    prev_status = note.status
    note.status = normalized
    if resolution_comment is not None:
        note.resolution_comment = resolution_comment.strip() or None

    if normalized == "in_progress" and prev_status in {"new", "reopened"}:
        note.assigned_at = note.assigned_at or datetime.now(timezone.utc)
    if normalized == "awaiting_user":
        note.resolved_at = datetime.now(timezone.utc)
    if normalized == "archived":
        note.archived_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(note)

    requester = note.requester
    if not requester:
        return note

    if normalized == "in_progress" and prev_status in {"new", "reopened"}:
        who = (operator_fio or "").strip() or "сотрудник ИТ"
        suffix = "" if who != "сотрудник ИТ" else " (Bitrix24)"
        await max_client.send_message(
            requester.max_user_id,
            f"🛠 Заявку #{note.id} взял в работу: <b>{who}</b>{suffix}.",
        )
    elif normalized == "awaiting_user" and prev_status != "awaiting_user":
        lines = [
            f"✅ Заявка #{note.id} выполнена (обновление из Bitrix24). "
            "Примите работу или сообщите, если проблема осталась."
        ]
        if note.resolution_comment:
            lines.extend(["", f"<b>Комментарий ИТ:</b> {note.resolution_comment}"])
        await max_client.send_message(
            requester.max_user_id,
            "\n".join(lines),
            attachments=service_note_user_review_keyboard(note.id),
        )
        await _send_delivery_files(requester.max_user_id, delivery_files or [])
    elif normalized == "archived" and prev_status != "archived":
        if note.resolution_comment:
            await max_client.send_message(
                requester.max_user_id,
                f"Заявка #{note.id} закрыта (Bitrix24).\n\n<b>Комментарий ИТ:</b> {note.resolution_comment}",
            )
        else:
            await max_client.send_message(
                requester.max_user_id,
                f"Заявка #{note.id} закрыта (Bitrix24).",
            )
        await _send_delivery_files(requester.max_user_id, delivery_files or [])
    elif normalized == "reopened" and prev_status != "reopened":
        await max_client.send_message(
            requester.max_user_id,
            f"Заявка #{note.id} возвращена в работу ИТ (Bitrix24).",
        )

    return note
