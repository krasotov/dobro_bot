from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.handlers.webhook import process_update
from app.max_client import max_client
from app.models import User
from app.services.audience_roles import IT_OPERATOR_ROLES, parse_audience_roles

logger = logging.getLogger(__name__)

# chat_id -> последний обработанный seq входящего сообщения пользователя
_watermark_seq: dict[int, int] = {}
_resolved_operator_chats: dict[int, int] = {}  # max_user_id -> chat_id


def _message_seq(message: dict[str, Any]) -> int:
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    seq = body.get("seq")
    try:
        return int(seq)
    except (TypeError, ValueError):
        return 0


_bot_user_id_cached: int | None = None


def _is_user_message(message: dict[str, Any], bot_id: int | None) -> bool:
    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    uid = sender.get("user_id")
    if uid is None:
        return False
    if sender.get("is_bot"):
        return False
    if bot_id is not None and int(uid) == int(bot_id):
        return False
    return True


def _synthetic_message_created(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "update_type": "message_created",
        "timestamp": message.get("timestamp"),
        "message": message,
    }


def _parse_extra_chat_ids() -> list[int]:
    raw = (getattr(settings, "dialog_inbox_poll_chat_ids", None) or "").strip()
    if not raw:
        return []
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            logger.warning("Invalid dialog_inbox_poll_chat_ids entry: %r", part)
    return result


def _user_is_operator_for_poll(user: User) -> bool:
    roles = set(parse_audience_roles(user.audience_roles))
    return bool(roles & (IT_OPERATOR_ROLES | {"operator", "appeals_operator", "garage_operator"}))


def remember_dialog_chat(max_user_id: int, chat_id: int | None) -> None:
    if max_user_id and chat_id is not None:
        _resolved_operator_chats[int(max_user_id)] = int(chat_id)


async def _resolve_chat_id_for_user(max_user_id: int) -> int | None:
    if max_user_id in _resolved_operator_chats:
        return _resolved_operator_chats[max_user_id]
    return None


async def collect_dialog_chat_ids(session: AsyncSession) -> list[int]:
    extra = _parse_extra_chat_ids()
    if extra:
        return sorted(set(extra))

    chat_ids: set[int] = set()
    result = await session.execute(select(User).where(User.is_registered.is_(True)))
    for user in result.scalars():
        if not _user_is_operator_for_poll(user):
            continue
        cid = await _resolve_chat_id_for_user(user.max_user_id)
        if cid is not None:
            chat_ids.add(cid)
    return sorted(chat_ids)


async def _init_watermark(chat_id: int) -> None:
    if chat_id in _watermark_seq:
        return
    try:
        data = await max_client._request(
            "GET",
            "/messages",
            params={"chat_id": chat_id, "count": 15},
        )
    except Exception:
        logger.exception("Failed to init dialog poll watermark chat_id=%s", chat_id)
        _watermark_seq[chat_id] = 0
        return
    messages = data.get("messages") or []
    max_bot_seq = 0
    global _bot_user_id_cached
    for message in messages:
        if not isinstance(message, dict):
            continue
        sender = message.get("sender") or {}
        if sender.get("is_bot") and _bot_user_id_cached is None:
            _bot_user_id_cached = int(sender.get("user_id") or 0) or None
        if sender.get("is_bot"):
            max_bot_seq = max(max_bot_seq, _message_seq(message))
    # Непрочитанные сообщения пользователя — те, что новее последнего сообщения бота.
    _watermark_seq[chat_id] = max_bot_seq
    logger.info("Dialog poll watermark chat_id=%s bot_seq=%s", chat_id, max_bot_seq)


async def poll_dialog_chat(session: AsyncSession, chat_id: int) -> int:
    global _bot_user_id_cached
    await _init_watermark(chat_id)
    try:
        data = await max_client._request(
            "GET",
            "/messages",
            params={"chat_id": chat_id, "count": 25},
        )
    except Exception:
        logger.exception("Dialog poll failed chat_id=%s", chat_id)
        return 0

    messages = sorted(
        [m for m in (data.get("messages") or []) if isinstance(m, dict)],
        key=_message_seq,
    )
    watermark = _watermark_seq.get(chat_id, 0)
    processed = 0
    bot_id = _bot_user_id_cached

    for message in messages:
        seq = _message_seq(message)
        if seq <= watermark:
            continue
        sender = message.get("sender") or {}
        if sender.get("is_bot") and bot_id is None:
            _bot_user_id_cached = int(sender.get("user_id") or 0) or None
            bot_id = _bot_user_id_cached
        if not _is_user_message(message, bot_id):
            watermark = max(watermark, seq)
            continue

        body = message.get("body") or {}
        text = (body.get("text") or "").strip()
        logger.info(
            "Dialog poll ingest chat_id=%s seq=%s from=%s text=%r",
            chat_id,
            seq,
            (sender.get("user_id")),
            text[:80],
        )
        watermark = max(watermark, seq)
        _watermark_seq[chat_id] = watermark
        update = _synthetic_message_created(message)
        await process_update(session, update)
        await session.commit()
        processed += 1

    _watermark_seq[chat_id] = watermark
    return processed


async def poll_operator_dialog_inboxes(session: AsyncSession) -> None:
    if not getattr(settings, "dialog_inbox_poll_enabled", True):
        return
    chat_ids = await collect_dialog_chat_ids(session)
    if not chat_ids:
        return
    total = 0
    for chat_id in chat_ids:
        total += await poll_dialog_chat(session, chat_id)
    if total:
        logger.info("Dialog poll processed %s user message(s)", total)
