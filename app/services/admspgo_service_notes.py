from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.models import ServiceNote, User
from app.services.appeal_attachments import download_max_attachment, guess_mime, parse_draft_attachments

logger = logging.getLogger(__name__)


def _api_config() -> tuple[str, str] | None:
    base = (settings.admspgo_api_url or "").strip().rstrip("/")
    token = (settings.admspgo_internal_token or "").strip()
    if not base or not token:
        return None
    return base, token


async def push_service_note_to_admspgo(
    note: ServiceNote,
    *,
    requester: User | None,
    operator: User | None = None,
) -> None:
    config = _api_config()
    if not config:
        logger.warning("admspgo service note sync skipped: API not configured")
        return

    base, token = config
    headers = {"Authorization": f"Bearer {token}"}

    metas = parse_draft_attachments(note.attachments)
    file_parts: list[tuple[str, tuple[str, bytes, str]]] = []
    for index, meta in enumerate(metas[:3], start=1):
        downloaded = await download_max_attachment(meta)
        if not downloaded:
            continue
        filename, content, mime = downloaded
        file_parts.append(("files", (filename or f"photo-{index}.jpg", content, mime or guess_mime(filename))))

    form_data: dict[str, str] = {
        "botNoteId": str(note.id),
        "department": note.department or "it",
        "roomNumber": note.room_number,
        "message": note.message[:8000],
        "status": note.status or "new",
    }
    if requester:
        form_data["requesterMaxUserId"] = str(requester.max_user_id)
        if requester.fio:
            form_data["requesterFio"] = requester.fio
        if requester.phone:
            form_data["requesterPhone"] = requester.phone
    if operator:
        form_data["operatorMaxUserId"] = str(operator.max_user_id)
        if operator.fio:
            form_data["operatorFio"] = operator.fio

    url = f"{base}/api/internal/max-bot/service-notes"
    try:
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
            if file_parts:
                response = await client.post(url, data=form_data, files=file_parts, headers=headers)
            else:
                response = await client.post(url, data=form_data, headers=headers)
    except Exception:
        logger.exception("Failed to POST service note %s to admspgo", note.id)
        return

    if response.status_code not in {200, 201}:
        logger.warning(
            "admspgo service note rejected note=%s status=%s body=%s",
            note.id,
            response.status_code,
            response.text[:300],
        )


async def patch_service_note_on_admspgo(
    note: ServiceNote,
    *,
    operator: User | None = None,
) -> None:
    config = _api_config()
    if not config:
        return

    base, token = config
    url = f"{base}/api/internal/max-bot/service-notes/{note.id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "status": note.status or "new",
        "resolutionComment": note.resolution_comment,
    }
    if operator:
        payload["operatorMaxUserId"] = operator.max_user_id
        payload["operatorFio"] = operator.fio or operator.max_name

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.patch(url, json=payload, headers=headers)
    except Exception:
        logger.exception("Failed to PATCH service note %s on admspgo", note.id)
        return

    if response.status_code not in {200, 201}:
        logger.warning(
            "admspgo service note patch failed note=%s status=%s body=%s",
            note.id,
            response.status_code,
            response.text[:300],
        )
