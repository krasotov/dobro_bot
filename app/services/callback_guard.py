from __future__ import annotations

import time

_CALLBACK_TTL_SEC = 120.0
_ACTION_DEBOUNCE_SEC = 2.0

_seen_callback_ids: dict[str, float] = {}
_recent_actions: dict[tuple[int, str], float] = {}

LIST_REFRESH_PAYLOADS = frozenset({"menu_transport_ops", "menu_appeals_ops"})


def _purge_expired(store: dict, ttl: float, now: float) -> None:
    expired = [key for key, ts in store.items() if now - ts > ttl]
    for key in expired:
        del store[key]


def is_duplicate_callback(callback_id: str) -> bool:
    now = time.monotonic()
    _purge_expired(_seen_callback_ids, _CALLBACK_TTL_SEC, now)
    if callback_id in _seen_callback_ids:
        return True
    _seen_callback_ids[callback_id] = now
    return False


def is_debounced_action(max_user_id: int, payload: str) -> bool:
    if payload not in LIST_REFRESH_PAYLOADS:
        return False
    now = time.monotonic()
    _purge_expired(_recent_actions, _ACTION_DEBOUNCE_SEC, now)
    key = (max_user_id, payload)
    if key in _recent_actions:
        return True
    _recent_actions[key] = now
    return False
