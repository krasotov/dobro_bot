from __future__ import annotations

import os

from app.max_client import MaxApiError, max_client

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".tiff", ".bmp", ".heic"}
MAX_IMAGE_SIZE = 50 * 1024 * 1024


def validate_broadcast_image(filename: str, content: bytes) -> str | None:
    if not content:
        return None

    ext = os.path.splitext(filename.lower())[1]
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return "Допустимые форматы: JPG, PNG, GIF, TIFF, BMP, HEIC"

    if len(content) > MAX_IMAGE_SIZE:
        return "Изображение не должно быть больше 50 МБ"

    return None


async def upload_broadcast_image(content: bytes, filename: str) -> str:
    try:
        return await max_client.upload_image(content, filename)
    except MaxApiError as exc:
        raise ValueError(f"Не удалось загрузить изображение в MAX: {exc.message[:300]}") from exc
