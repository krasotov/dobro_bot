from __future__ import annotations

import json
import mimetypes
from typing import Any

MAX_APPEAL_ATTACHMENTS = 3


def parse_draft_attachments(raw: str | None) -> list[dict[str, Any]]:
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)][:MAX_APPEAL_ATTACHMENTS]
    except json.JSONDecodeError:
        pass
    return []


def serialize_draft_attachments(items: list[dict[str, Any]]) -> str | None:
    if not items:
        return None
    return json.dumps(items[:MAX_APPEAL_ATTACHMENTS], ensure_ascii=False)


def extract_attachment_meta(attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for attachment in attachments:
        if attachment.get("type") not in {"image", "file", "photo"}:
            continue
        payload = attachment.get("payload") or {}
        url = payload.get("url") or payload.get("file_url")
        token = payload.get("token")
        name = payload.get("filename") or payload.get("name") or "photo.jpg"
        if url or token:
            return {"url": url, "token": token, "filename": str(name)[:200]}
    return None


def guess_mime(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"
