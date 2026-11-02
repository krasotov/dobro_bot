from __future__ import annotations

from typing import Any


def resolve_callback_payload(update: dict[str, Any]) -> str:
    """Строка payload из message_callback (MAX кладёт её в callback.payload)."""
    callback = update.get("callback") if isinstance(update.get("callback"), dict) else {}
    raw = callback.get("payload")
    if raw is not None and str(raw).strip():
        return str(raw).strip()

    data = callback.get("data")
    if data is not None and str(data).strip():
        return str(data).strip()

    callback_id = str(callback.get("callback_id") or "")
    message = update.get("message") if isinstance(update.get("message"), dict) else {}
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    for attachment in body.get("attachments") or []:
        if not isinstance(attachment, dict) or attachment.get("type") != "inline_keyboard":
            continue
        att_cid = str(attachment.get("callback_id") or "")
        if callback_id and att_cid and att_cid != callback_id:
            continue
        payload = attachment.get("payload")
        if isinstance(payload, str) and payload.strip():
            return payload.strip()
        buttons = (payload or {}).get("buttons") if isinstance(payload, dict) else None
        if not buttons:
            continue
        for row in buttons:
            if not isinstance(row, list):
                continue
            for button in row:
                if not isinstance(button, dict):
                    continue
                btn_payload = button.get("payload")
                if btn_payload is not None and str(btn_payload).strip():
                    return str(btn_payload).strip()

    return ""
