from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.bot_menu import send_main_menu
from app.max_client import appeal_attach_keyboard, appeal_cancel_keyboard, max_client, utilities_disclaimer_keyboard
from app.models import User
from app.services.admspgo_appeals import download_attachment, submit_max_appeal
from app.services.appeal_attachments import (
    MAX_APPEAL_ATTACHMENTS,
    extract_attachment_meta,
    parse_draft_attachments,
    serialize_draft_attachments,
)

APPEAL_FLOW_NONE = "none"
APPEAL_FLOW_KIND = "kind"
APPEAL_FLOW_TOPIC = "topic"
APPEAL_FLOW_DISCLAIMER = "disclaimer"
APPEAL_FLOW_MESSAGE = "message"
APPEAL_FLOW_ATTACH = "attach"

APPEAL_CATEGORY_GENERAL = "GENERAL"
APPEAL_CATEGORY_UTILITIES = "UTILITIES"

APPEAL_TOPICS: list[tuple[str, str]] = [
    ("UTILITIES", "ЖКХ"),
    ("ROADS", "Дороги и транспорт"),
    ("SOCIAL", "Образ. и соц. сфера"),
    ("IMPROVEMENT", "Благоустройство"),
    ("LAND", "Земля и имущество"),
    ("CONSTRUCTION", "Стройка"),
    ("SAFETY", "Безопасность"),
    ("GENERAL", "Прочее"),
]

APPEAL_TOPIC_LABELS = dict(APPEAL_TOPICS)
VALID_APPEAL_CATEGORIES = frozenset(APPEAL_TOPIC_LABELS)

SUBMIT_WORDS = {"отправить", "готово", "send", "submit"}
CONTINUE_WORDS = {"продолжить", "continue", "да", "ok"}


def _address_full(user: User) -> str | None:
    text = (user.address_normalized or user.address or "").strip()
    return text or None


def _api_category(user: User) -> str:
    raw = (user.appeal_draft_category or APPEAL_CATEGORY_GENERAL).strip().upper()
    if raw.lower() == "general":
        return "GENERAL"
    if raw.lower() == "utilities":
        return "UTILITIES"
    if raw in VALID_APPEAL_CATEGORIES:
        return raw
    return APPEAL_CATEGORY_GENERAL


def _topic_label(category: str | None) -> str:
    return APPEAL_TOPIC_LABELS.get((category or APPEAL_CATEGORY_GENERAL).upper(), "Обращение")


def _clear_draft(user: User) -> None:
    user.appeal_flow_state = APPEAL_FLOW_NONE
    user.appeal_draft_subject = None
    user.appeal_draft_message = None
    user.appeal_draft_attachments = None
    user.appeal_draft_category = APPEAL_CATEGORY_GENERAL


def _in_appeal_flow(user: User) -> bool:
    return (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_NONE


def appeal_kind_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [{"type": "callback", "text": "Обращение гражданина", "payload": "appeal_kind_citizen"}],
                    [{"type": "callback", "text": "Служебная записка", "payload": "appeal_kind_service_note"}],
                    [{"type": "callback", "text": "Отмена", "payload": "appeal_cancel"}],
                ],
            },
        }
    ]


def appeal_topic_keyboard() -> list[dict[str, Any]]:
    rows: list[list[dict[str, str]]] = []
    for index in range(0, len(APPEAL_TOPICS), 2):
        row = [
            {
                "type": "callback",
                "text": APPEAL_TOPICS[index][1],
                "payload": f"appeal_topic_{APPEAL_TOPICS[index][0]}",
            }
        ]
        if index + 1 < len(APPEAL_TOPICS):
            row.append(
                {
                    "type": "callback",
                    "text": APPEAL_TOPICS[index + 1][1],
                    "payload": f"appeal_topic_{APPEAL_TOPICS[index + 1][0]}",
                }
            )
        rows.append(row)
    rows.append([{"type": "callback", "text": "Отмена", "payload": "appeal_cancel"}])
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


async def _start_message_step(session: AsyncSession, user: User) -> None:
    from app.texts import APPEAL_ASK_MESSAGE

    user.appeal_flow_state = APPEAL_FLOW_MESSAGE
    await session.commit()
    topic = _topic_label(user.appeal_draft_category)
    await max_client.send_message(
        user.max_user_id,
        f"<b>{topic}</b>\n\n{APPEAL_ASK_MESSAGE}",
        attachments=appeal_cancel_keyboard(),
        menu_bar=False,
    )


async def _show_topic_picker(session: AsyncSession, user: User) -> None:
    from app.texts import APPEAL_ASK_TOPIC

    user.appeal_flow_state = APPEAL_FLOW_TOPIC
    user.appeal_draft_category = APPEAL_CATEGORY_GENERAL
    await session.commit()
    await max_client.send_message(
        user.max_user_id,
        APPEAL_ASK_TOPIC,
        attachments=appeal_topic_keyboard(),
    )


async def _begin_category(session: AsyncSession, user: User, category: str) -> None:
    normalized = category.strip().upper()
    if normalized not in VALID_APPEAL_CATEGORIES:
        normalized = APPEAL_CATEGORY_GENERAL

    user.appeal_draft_category = normalized
    user.appeal_draft_subject = _topic_label(normalized)

    if normalized == APPEAL_CATEGORY_UTILITIES:
        from app.texts import UTILITIES_DISCLAIMER

        user.appeal_flow_state = APPEAL_FLOW_DISCLAIMER
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            UTILITIES_DISCLAIMER,
            attachments=utilities_disclaimer_keyboard(),
        )
        return

    await _start_message_step(session, user)


async def try_start_appeal_flow(
    session: AsyncSession,
    user: User,
    *,
    category: str | None = None,
) -> bool:
    from app.texts import APPEAL_NEED_REGISTRATION, PROFILE_FINISH_EDIT

    if not user.is_registered:
        await max_client.send_message(user.max_user_id, APPEAL_NEED_REGISTRATION)
        return True

    from app.services.profile_flow import in_profile_flow

    if in_profile_flow(user):
        from app.max_client import profile_cancel_keyboard

        await max_client.send_message(
            user.max_user_id,
            PROFILE_FINISH_EDIT,
            attachments=profile_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    if _in_appeal_flow(user):
        await max_client.send_message(
            user.max_user_id,
            "Вы уже заполняете обращение. Продолжите ввод или отмените.",
            attachments=appeal_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    user.appeal_draft_subject = None
    user.appeal_draft_message = None
    user.appeal_draft_attachments = None

    if category:
        await _begin_category(session, user, category)
        return True

    from app.services.audience_roles import user_can_create_service_note

    if user_can_create_service_note(user):
        from app.texts import APPEAL_ASK_KIND

        user.appeal_flow_state = APPEAL_FLOW_KIND
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            APPEAL_ASK_KIND,
            attachments=appeal_kind_keyboard(),
        )
        return True

    await _show_topic_picker(session, user)
    return True


async def try_handle_appeal_callback(session: AsyncSession, user: User, payload: str) -> bool:
    if payload == "appeal_kind_citizen":
        if (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_KIND:
            return False
        await _show_topic_picker(session, user)
        return True

    if payload == "appeal_kind_service_note":
        if (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_KIND:
            return False
        user.appeal_flow_state = APPEAL_FLOW_NONE
        await session.commit()
        from app.services.service_note_flow import try_start_service_note_flow

        await try_start_service_note_flow(session, user)
        return True

    if payload.startswith("appeal_topic_"):
        if (user.appeal_flow_state or APPEAL_FLOW_NONE) not in {APPEAL_FLOW_TOPIC, APPEAL_FLOW_NONE}:
            return False
        category = payload.removeprefix("appeal_topic_")
        await _begin_category(session, user, category)
        return True

    if payload == "utilities_continue":
        if (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_DISCLAIMER:
            return False
        await _start_message_step(session, user)
        return True

    if payload in {"utilities_cancel", "appeal_cancel"}:
        if not _in_appeal_flow(user):
            return False
        await cancel_appeal_flow(session, user)
        return True

    if payload == "appeal_submit":
        if (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_ATTACH:
            return False
        await _submit_appeal(session, user)
        return True

    return False


async def cancel_appeal_flow(session: AsyncSession, user: User) -> None:
    from app.texts import APPEAL_CANCELLED

    _clear_draft(user)
    await session.commit()
    await max_client.send_message(user.max_user_id, APPEAL_CANCELLED)
    await send_main_menu(user.max_user_id, registered=True)


async def _submit_appeal(session: AsyncSession, user: User) -> None:
    from app.texts import APPEAL_INVALID_MESSAGE, APPEAL_SENT, APPEAL_START_HINT

    address = _address_full(user)
    if not address:
        _clear_draft(user)
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            "В профиле нет адреса — обратитесь в администрацию для обновления данных.",
        )
        return

    category = _api_category(user)
    message = (user.appeal_draft_message or "").strip()
    if len(message) < 10:
        await max_client.send_message(user.max_user_id, APPEAL_INVALID_MESSAGE)
        return

    subject = (user.appeal_draft_subject or _topic_label(category)).strip()[:200]
    payload: dict[str, str | int] = {
        "maxUserId": user.max_user_id,
        "subject": subject,
        "message": message[:8000],
        "reporterName": user.fio or "Житель",
        "reporterPhone": user.phone or "",
        "reporterDistrict": user.district or "",
        "addressFull": address,
        "addressLabel": "MAX",
    }
    payload["category"] = category

    metas = parse_draft_attachments(user.appeal_draft_attachments)
    files: list[tuple[str, bytes, str]] = []
    for meta in metas:
        downloaded = await download_attachment(meta)
        if downloaded:
            files.append(downloaded)

    ok, err = await submit_max_appeal(payload, files if files else None)

    if ok:
        _clear_draft(user)
        await session.commit()
        await max_client.send_message(user.max_user_id, APPEAL_SENT)
        await send_main_menu(user.max_user_id, registered=True)
    else:
        user.appeal_flow_state = APPEAL_FLOW_ATTACH
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            err or APPEAL_START_HINT,
            attachments=appeal_attach_keyboard(),
            menu_bar=False,
        )


async def handle_appeal_flow_message(
    session: AsyncSession,
    user: User,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    from app.texts import (
        APPEAL_ASK_ATTACHMENTS,
        APPEAL_ATTACHMENT_LIMIT,
        APPEAL_ATTACHMENT_SAVED,
        APPEAL_INVALID_MESSAGE,
    )

    state = (user.appeal_flow_state or APPEAL_FLOW_NONE).lower()
    if state == APPEAL_FLOW_NONE:
        return False

    attachments = attachments or []
    lowered = text.lower().strip()
    if lowered in {"отмена", "cancel"}:
        await cancel_appeal_flow(session, user)
        return True

    if state == APPEAL_FLOW_KIND:
        from app.texts import APPEAL_ASK_KIND

        await max_client.send_message(
            user.max_user_id,
            APPEAL_ASK_KIND,
            attachments=appeal_kind_keyboard(),
            menu_bar=False,
        )
        return True

    if state == APPEAL_FLOW_TOPIC:
        await max_client.send_message(
            user.max_user_id,
            "Выберите тематику кнопкой ниже.",
            attachments=appeal_topic_keyboard(),
            menu_bar=False,
        )
        return True

    if state == APPEAL_FLOW_DISCLAIMER:
        if lowered in CONTINUE_WORDS:
            await _start_message_step(session, user)
            return True
        await max_client.send_message(
            user.max_user_id,
            "Нажмите <b>Продолжить</b> под сообщением или <b>Отмена</b>.",
            attachments=utilities_disclaimer_keyboard(),
            menu_bar=False,
        )
        return True

    if state == APPEAL_FLOW_ATTACH:
        meta = extract_attachment_meta(attachments)
        if meta:
            items = parse_draft_attachments(user.appeal_draft_attachments)
            if len(items) >= MAX_APPEAL_ATTACHMENTS:
                await max_client.send_message(
                    user.max_user_id,
                    APPEAL_ATTACHMENT_LIMIT,
                    attachments=appeal_attach_keyboard(),
                    menu_bar=False,
                )
                return True
            items.append(meta)
            user.appeal_draft_attachments = serialize_draft_attachments(items)
            await session.commit()
            await max_client.send_message(
                user.max_user_id,
                APPEAL_ATTACHMENT_SAVED.format(count=len(items)),
                attachments=appeal_attach_keyboard(),
                menu_bar=False,
            )
            return True

        if lowered in SUBMIT_WORDS or (not text and not attachments):
            await _submit_appeal(session, user)
            return True

        if text:
            await max_client.send_message(
                user.max_user_id,
                "Приложите фото через 📎 или нажмите <b>Отправить</b>.",
                attachments=appeal_attach_keyboard(),
                menu_bar=False,
            )
            return True
        return True

    if state == APPEAL_FLOW_MESSAGE:
        message = text.strip()
        if len(message) < 10:
            await max_client.send_message(
                user.max_user_id,
                APPEAL_INVALID_MESSAGE,
                attachments=appeal_cancel_keyboard(),
                menu_bar=False,
            )
            return True

        user.appeal_draft_message = message[:8000]
        user.appeal_flow_state = APPEAL_FLOW_ATTACH
        user.appeal_draft_attachments = None
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            APPEAL_ASK_ATTACHMENTS,
            attachments=appeal_attach_keyboard(),
            menu_bar=False,
        )
        return True

    _clear_draft(user)
    await session.commit()
    return False


async def send_appeal_hint(user: User) -> None:
    from app.texts import APPEAL_START_HINT

    await max_client.send_message(user.max_user_id, APPEAL_START_HINT)
