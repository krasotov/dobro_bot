from __future__ import annotations

import logging
from typing import Any

from app.max_client import max_client
from app.services.appeal_attachments import (
    _looks_like_image,
    download_max_attachment,
    guess_mime,
    parse_draft_attachments,
    serialize_draft_attachments,
)
from app.services.service_note_service import parse_note_attachments

logger = logging.getLogger(__name__)


async def materialize_service_note_attachments(raw: str | None) -> str | None:
    """
    При отправке заявки: скачать файлы заявителя и загрузить через API бота.
    В БД остаются только токены, пригодные для отправки оператору ИТ позже.
    """
    items = parse_draft_attachments(raw)
    if not items:
        return None

    stored: list[dict[str, Any]] = []
    for meta in items:
        persisted = await _persist_one_attachment(meta)
        if persisted:
            stored.append(persisted)

    return serialize_draft_attachments(stored)


async def _persist_one_attachment(meta: dict[str, Any]) -> dict[str, Any] | None:
    filename = str(meta.get("filename") or "photo.jpg")[:200]
    downloaded = await download_max_attachment(meta)
    if downloaded:
        filename, data, mime = downloaded
        if not _looks_like_image(filename, mime):
            mime = guess_mime(filename)
        if _looks_like_image(filename, mime):
            try:
                token = await max_client.upload_image(data, filename)
                return {"token": token, "filename": filename, "kind": "image"}
            except Exception:
                logger.exception("Failed to upload service note image to MAX")
        else:
            try:
                token = await max_client.upload_file(data, filename)
                return {"token": token, "filename": filename, "kind": "file"}
            except Exception:
                logger.exception("Failed to upload service note file to MAX")

    # Токен из входящего сообщения нельзя надёжно переслать другому пользователю — не сохраняем.
    return None


async def send_note_attachments_to_user(max_user_id: int, attachments_raw: str | None) -> int:
    """Пересылает сохранённые вложения заявки пользователю в MAX. Возвращает число отправленных."""
    metas = parse_note_attachments(attachments_raw)
    sent = 0
    for index, meta in enumerate(metas[:3], start=1):
        token = meta.get("token")
        kind = meta.get("kind") or "image"
        filename = str(meta.get("filename") or f"attachment-{index}")

        if token:
            att_type = "file" if kind == "file" else "image"
            try:
                await max_client.send_message(
                    max_user_id,
                    f"📎 {filename}",
                    attachments=[{"type": att_type, "payload": {"token": str(token)}}],
                    menu_bar=False,
                )
                sent += 1
                continue
            except Exception:
                logger.exception("Failed to send service note attachment by stored token")

        downloaded = await download_max_attachment(meta)
        if not downloaded:
            continue
        fn, data, mime = downloaded
        if not _looks_like_image(fn, mime):
            continue
        try:
            new_token = await max_client.upload_image(data, fn)
            await max_client.send_message(
                max_user_id,
                f"📎 {fn}",
                attachments=[{"type": "image", "payload": {"token": new_token}}],
                menu_bar=False,
            )
            sent += 1
        except Exception:
            logger.exception("Failed to re-upload service note attachment for operator")

    return sent
