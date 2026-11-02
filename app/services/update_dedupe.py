from __future__ import annotations

import time
from typing import Any

_EVENT_TTL_SEC = 600.0
_USER_CMD_DEBOUNCE_SEC = 2.5

_seen_event_keys: dict[str, float] = {}
_recent_user_commands: dict[tuple[int, str], float] = {}

QUICK_USER_COMMANDS = frozenset(
    {
        "меню",
        "menu",
        "start",
        "/menu",
        "заявки ит",
        "it",
        "it заявки",
        "заявки it",
    }
)


def _purge(store: dict, ttl: float, now: float) -> None:
    expired = [k for k, ts in store.items() if now - ts > ttl]
    for k in expired:
        del store[k]


def update_event_key(update: dict[str, Any]) -> str | None:
    update_type = update.get("update_type")
    if update_type == "message_callback":
        callback = update.get("callback") if isinstance(update.get("callback"), dict) else {}
        callback_id = callback.get("callback_id")
        if callback_id:
            return f"cb:{callback_id}"
        return None

    if update_type == "message_created":
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        body = message.get("body") if isinstance(message.get("body"), dict) else {}
        mid = body.get("mid")
        if mid:
            return f"mid:{mid}"
        recipient = message.get("recipient") if isinstance(message.get("recipient"), dict) else {}
        chat_id = recipient.get("chat_id")
        seq = body.get("seq")
        if chat_id is not None and seq is not None:
            return f"seq:{chat_id}:{seq}"
    return None


def is_duplicate_update(update: dict[str, Any]) -> bool:
    key = update_event_key(update)
    if not key:
        return False
    now = time.monotonic()
    _purge(_seen_event_keys, _EVENT_TTL_SEC, now)
    return key in _seen_event_keys


def mark_update_seen(update: dict[str, Any]) -> None:
    key = update_event_key(update)
    if not key:
        return
    _seen_event_keys[key] = time.monotonic()


def should_skip_user_command(max_user_id: int, command: str) -> bool:
    """Повтор «меню» / «заявки ит» с кнопок подряд — один ответ."""
    cmd = command.strip().lower()
    if cmd not in QUICK_USER_COMMANDS:
        return False
    now = time.monotonic()
    _purge(_recent_user_commands, _USER_CMD_DEBOUNCE_SEC, now)
    key = (max_user_id, cmd)
    if key in _recent_user_commands:
        return True
    _recent_user_commands[key] = now
    return False
