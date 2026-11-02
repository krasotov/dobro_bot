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


def _looks_like_image(filename: str, mime: str) -> bool:
    if mime.startswith("image/"):
        return True
    lower = filename.lower()
    return lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tiff"))


async def download_max_attachment(meta: dict[str, Any]) -> tuple[str, bytes, str] | None:
    """Скачивает вложение MAX по url (с Authorization для CDN MAX)."""
    from app.config import settings

    url = meta.get("url") or meta.get("file_url")
    if not url or not isinstance(url, str):
        return None
    filename = str(meta.get("filename") or "photo.jpg")[:200]
    headers: dict[str, str] = {}
    if "max.ru" in url:
        headers["Authorization"] = settings.max_bot_token
    try:
        import httpx

        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers or None)
            response.raise_for_status()
            content = response.content
            if len(content) > 10 * 1024 * 1024:
                return None
            mime = response.headers.get("content-type", "").split(";")[0].strip() or guess_mime(filename)
            return filename, content, mime
    except Exception:
        return None
