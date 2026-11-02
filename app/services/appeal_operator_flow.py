from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import appeal_operator_cancel_keyboard, max_client
from app.models import User
from app.services.admspgo_appeals import fetch_appeal, fetch_open_appeals, update_appeal
from app.services.audience_roles import user_is_appeals_operator
from app.texts import (
    APPEAL_OPERATOR_ASK_RESPONSE,
    APPEAL_OPERATOR_CANCELLED,
    APPEAL_OPERATOR_NEED_ROLE,
    APPEAL_OPERATOR_NO_APPEALS,
    APPEAL_OPERATOR_SAVED,
    APPEAL_OPERATOR_SERVICE_ERROR,
)

APPEAL_OPERATOR_FLOW_NONE = "none"
APPEAL_OPERATOR_FLOW_AWAIT_RESPONSE = "await_response"

APPEAL_STATUS_LABELS = {
    "NEW": "Новое",
    "IN_PROGRESS": "В работе",
    "RESOLVED": "Решено",
    "REJECTED": "Отклонено",
}

APPEAL_CATEGORY_LABELS = {
    "GENERAL": "Прочее",
    "UTILITIES": "ЖКХ",
    "ROADS": "Дороги и транспорт",
    "SOCIAL": "Образование, культура, соц. сфера",
    "IMPROVEMENT": "Благоустройство",
    "LAND": "Земля и имущество",
    "CONSTRUCTION": "Стройка и градостроение",
    "SAFETY": "Безопасность и экология",
}


def in_appeal_operator_flow(user: User) -> bool:
    return (user.appeal_operator_flow_state or APPEAL_OPERATOR_FLOW_NONE) != APPEAL_OPERATOR_FLOW_NONE


def reset_appeal_operator_flow(user: User) -> None:
    user.appeal_operator_flow_state = APPEAL_OPERATOR_FLOW_NONE
    user.appeal_operator_appeal_id = None
    user.appeal_operator_target_status = None


MSK = ZoneInfo("Europe/Moscow")


def _parse_appeal_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_datetime(raw: str | None) -> str:
    dt = _parse_appeal_datetime(raw)
    if not dt:
        return "—"
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def _format_list_time(raw: str | None) -> str:
    dt = _parse_appeal_datetime(raw)
    if not dt:
        return "—"
    return dt.astimezone(MSK).strftime("%d.%m %H:%M")


def _short_fio(name: str | None) -> str:
    if not name or not str(name).strip():
        return "—"
    parts = str(name).strip().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return parts[0][:14]


def _short_topic(appeal: dict) -> str:
    subject = str(appeal.get("subject") or "").strip()
    if subject and subject.lower() not in {"без темы", "—", "-"}:
        return subject[:20]
    category = APPEAL_CATEGORY_LABELS.get(str(appeal.get("category")), "Прочее")
    return category[:20]


def _appeal_list_button_label(appeal: dict) -> str:
    status = str(appeal.get("status") or "")
    prefix = "🆕 " if status == "NEW" else "▶ " if status == "IN_PROGRESS" else ""
    when = _format_list_time(appeal.get("createdAt"))
    who = _short_fio(appeal.get("reporterName"))
    topic = _short_topic(appeal)
    label = f"{prefix}{when} · {who} · {topic}"
    return label[:64].strip()


def _format_appeals_queue_summary(appeals: list[dict]) -> str:
    lines = [
        "<b>Обращения в работе</b>",
        f"Открытых: {len(appeals)}",
        "",
        "Нажмите кнопку ниже, чтобы открыть карточку:",
        "",
    ]
    for index, appeal in enumerate(appeals[:10], start=1):
        status = APPEAL_STATUS_LABELS.get(str(appeal.get("status")), "—")
        when = _format_datetime(appeal.get("createdAt"))
        who = appeal.get("reporterName") or "—"
        topic = _short_topic(appeal)
        lines.append(f"{index}. <b>{status}</b> · {when} · {who} · {topic}")
    if len(appeals) > 10:
        lines.append(f"\n<i>Показаны первые 10 из {len(appeals)}.</i>")
    return "\n".join(lines)


def _format_appeal_detail(appeal: dict) -> str:
    status = APPEAL_STATUS_LABELS.get(str(appeal.get("status")), str(appeal.get("status")))
    category = APPEAL_CATEGORY_LABELS.get(str(appeal.get("category")), "Обращение")
    reporter = appeal.get("reporterName") or "—"
    phone = appeal.get("reporterPhone") or "—"
    district = appeal.get("reporterDistrict")
    address = appeal.get("addressFull") or "—"
    subject = appeal.get("subject") or "—"
    message = appeal.get("message") or "—"
    response = (appeal.get("response") or "").strip()
    appeal_id = appeal.get("id") or "—"

    lines = [
        f"<b>{category}</b> · {status}",
        f"№ <code>{appeal_id}</code>",
        f"Дата: {_format_datetime(appeal.get('createdAt'))}",
        f"Тема: {subject}",
        f"Адрес: {address}",
        f"Заявитель: {reporter}",
        f"Телефон: {phone}",
    ]
    if district:
        lines.append(f"Район: {district}")
    lines.append("")
    lines.append(message)
    if response:
        lines.extend(["", f"<b>Ответ:</b> {response}"])

    attachments = appeal.get("attachments")
    if isinstance(attachments, list) and attachments:
        lines.append("")
        lines.append("<b>Вложения:</b>")
        for idx, item in enumerate(attachments[:5], start=1):
            if not isinstance(item, dict):
                continue
            url = item.get("filePath") or item.get("url")
            if url:
                lines.append(f'{idx}. <a href="{url}">фото</a>')

    return "\n".join(lines)


def _detail_keyboard(appeal: dict) -> list[dict]:
    appeal_id = str(appeal.get("id"))
    status = str(appeal.get("status"))
    rows: list[list[dict]] = []

    if status == "NEW":
        rows.append(
            [
                {
                    "type": "callback",
                    "text": "В работу",
                    "payload": f"appeal_op_progress_{appeal_id}",
                }
            ]
        )
    if status in {"NEW", "IN_PROGRESS"}:
        rows.append(
            [
                {
                    "type": "callback",
                    "text": "Решено",
                    "payload": f"appeal_op_resolve_{appeal_id}",
                },
                {
                    "type": "callback",
                    "text": "Отклонить",
                    "payload": f"appeal_op_reject_{appeal_id}",
                },
            ]
        )
    rows.append(
        [
            {"type": "callback", "text": "← К списку", "payload": "menu_appeals_ops"},
            {"type": "callback", "text": "Меню", "payload": "menu_open"},
        ]
    )
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def _list_keyboard(appeals: list[dict]) -> list[dict]:
    rows: list[list[dict]] = []
    for index, appeal in enumerate(appeals[:10], start=1):
        appeal_id = str(appeal.get("id"))
        label = _appeal_list_button_label(appeal)
        if len(appeals) > 1:
            label = f"{index}. {label}"[:64]
        rows.append(
            [
                {
                    "type": "callback",
                    "text": label,
                    "payload": f"appeal_op_view_{appeal_id}",
                }
            ]
        )
    rows.append(
        [
            {"type": "callback", "text": "Обновить", "payload": "menu_appeals_ops"},
            {"type": "callback", "text": "← Меню", "payload": "menu_open"},
        ]
    )
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


async def send_operator_appeals_list(user: User, *, callback_id: str | None = None) -> None:
    if not user_is_appeals_operator(user):
        await max_client.send_message(user.max_user_id, APPEAL_OPERATOR_NEED_ROLE)
        return

    appeals, error = await fetch_open_appeals()
    if error:
        await max_client.send_message(user.max_user_id, f"{APPEAL_OPERATOR_SERVICE_ERROR}\n{error}")
        return

    if not appeals:
        text = APPEAL_OPERATOR_NO_APPEALS
        attachments = None
    else:
        text = _format_appeals_queue_summary(appeals)
        attachments = _list_keyboard(appeals)

    if callback_id:
        body: dict = {"text": text, "format": "html"}
        if attachments:
            body["attachments"] = attachments
        await max_client.answer_callback(
            callback_id,
            message=body,
            notification="Принято",
        )
        return

    await max_client.send_message(user.max_user_id, text, attachments=attachments)


async def _show_appeal_detail(user: User, appeal_id: str) -> None:
    appeal, error = await fetch_appeal(appeal_id)
    if error or not appeal:
        await max_client.send_message(user.max_user_id, error or APPEAL_OPERATOR_SERVICE_ERROR)
        return

    await max_client.send_message(
        user.max_user_id,
        _format_appeal_detail(appeal),
        attachments=_detail_keyboard(appeal),
    )


async def try_handle_appeal_operator_callback(
    session: AsyncSession,
    user: User,
    payload: str,
    *,
    callback_id: str | None = None,
) -> bool:
    if payload == "menu_appeals_ops":
        reset_appeal_operator_flow(user)
        await session.commit()
        await send_operator_appeals_list(user, callback_id=callback_id)
        return True

    if payload == "appeal_operator_cancel":
        if not in_appeal_operator_flow(user):
            return False
        reset_appeal_operator_flow(user)
        await session.commit()
        await max_client.send_message(user.max_user_id, APPEAL_OPERATOR_CANCELLED)
        await send_operator_appeals_list(user)
        return True

    if payload.startswith("appeal_op_view_"):
        if not user_is_appeals_operator(user):
            await max_client.send_message(user.max_user_id, APPEAL_OPERATOR_NEED_ROLE)
            return True
        appeal_id = payload.removeprefix("appeal_op_view_")
        reset_appeal_operator_flow(user)
        await session.commit()
        await _show_appeal_detail(user, appeal_id)
        return True

    if payload.startswith("appeal_op_progress_"):
        if not user_is_appeals_operator(user):
            await max_client.send_message(user.max_user_id, APPEAL_OPERATOR_NEED_ROLE)
            return True
        appeal_id = payload.removeprefix("appeal_op_progress_")
        ok, error = await update_appeal(appeal_id, status="IN_PROGRESS")
        if not ok:
            await max_client.send_message(user.max_user_id, error or APPEAL_OPERATOR_SERVICE_ERROR)
            return True
        await max_client.send_message(user.max_user_id, "Обращение переведено <b>в работу</b>.")
        await _show_appeal_detail(user, appeal_id)
        return True

    if payload.startswith("appeal_op_resolve_"):
        if not user_is_appeals_operator(user):
            await max_client.send_message(user.max_user_id, APPEAL_OPERATOR_NEED_ROLE)
            return True
        appeal_id = payload.removeprefix("appeal_op_resolve_")
        user.appeal_operator_flow_state = APPEAL_OPERATOR_FLOW_AWAIT_RESPONSE
        user.appeal_operator_appeal_id = appeal_id
        user.appeal_operator_target_status = "RESOLVED"
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            APPEAL_OPERATOR_ASK_RESPONSE,
            attachments=appeal_operator_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    if payload.startswith("appeal_op_reject_"):
        if not user_is_appeals_operator(user):
            await max_client.send_message(user.max_user_id, APPEAL_OPERATOR_NEED_ROLE)
            return True
        appeal_id = payload.removeprefix("appeal_op_reject_")
        user.appeal_operator_flow_state = APPEAL_OPERATOR_FLOW_AWAIT_RESPONSE
        user.appeal_operator_appeal_id = appeal_id
        user.appeal_operator_target_status = "REJECTED"
        await session.commit()
        await max_client.send_message(
            user.max_user_id,
            APPEAL_OPERATOR_ASK_RESPONSE,
            attachments=appeal_operator_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    return False


async def handle_appeal_operator_message(session: AsyncSession, user: User, text: str) -> bool:
    if not in_appeal_operator_flow(user):
        return False

    if not user_is_appeals_operator(user):
        reset_appeal_operator_flow(user)
        await session.commit()
        return False

    normalized = text.strip()
    if normalized.lower() == "отмена":
        reset_appeal_operator_flow(user)
        await session.commit()
        await max_client.send_message(user.max_user_id, APPEAL_OPERATOR_CANCELLED)
        await send_operator_appeals_list(user)
        return True

    if len(normalized) < 3:
        await max_client.send_message(
            user.max_user_id,
            "Ответ слишком короткий. Введите не менее 3 символов.",
            attachments=appeal_operator_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    appeal_id = user.appeal_operator_appeal_id
    target_status = user.appeal_operator_target_status
    if not appeal_id or target_status not in {"RESOLVED", "REJECTED"}:
        reset_appeal_operator_flow(user)
        await session.commit()
        return False

    ok, error = await update_appeal(appeal_id, status=target_status, response=normalized)
    reset_appeal_operator_flow(user)
    await session.commit()

    if not ok:
        await max_client.send_message(user.max_user_id, error or APPEAL_OPERATOR_SERVICE_ERROR)
        return True

    await max_client.send_message(user.max_user_id, APPEAL_OPERATOR_SAVED)
    await _show_appeal_detail(user, appeal_id)
    return True
