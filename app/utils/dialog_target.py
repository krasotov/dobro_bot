from __future__ import annotations

from typing import Any


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_chat_id(update: dict[str, Any]) -> int | None:
    from app.services.channel_news import resolve_update_chat_id

    chat_id = resolve_update_chat_id(update)
    if chat_id is not None:
        return chat_id

    callback = update.get("callback") if isinstance(update.get("callback"), dict) else {}
    chat_id = _as_int(callback.get("chat_id"))
    if chat_id is not None:
        return chat_id

    cb_message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    if cb_message:
        return resolve_update_chat_id({"message": cb_message})

    return None


def dialog_target_from_update(update: dict[str, Any]) -> tuple[int | None, int | None]:
    """(chat_id, max_user_id) для ответа пользователю."""
    callback = update.get("callback") or {}
    cb_user = callback.get("user") if isinstance(callback.get("user"), dict) else {}
    user_id = _as_int(cb_user.get("user_id") or cb_user.get("id"))

    message = update.get("message") if isinstance(update.get("message"), dict) else {}
    chat_id = _resolve_chat_id(update)

    if user_id is None:
        for candidate in (
            update.get("user"),
            message.get("sender"),
        ):
            if isinstance(candidate, dict):
                user_id = _as_int(candidate.get("user_id") or candidate.get("id"))
                if user_id is not None:
                    break

    return chat_id, user_id
