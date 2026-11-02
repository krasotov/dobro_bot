from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import max_client, transport_cancel_keyboard, transport_confirm_keyboard
from app.models import TransportOrder, User
from app.services.audience_roles import user_can_order_transport
from app.services.transport_pickers import (
    build_calendar_keyboard,
    build_time_keyboard,
    calendar_caption,
    has_time_slots,
    parse_day_payload,
    parse_month_payload,
    parse_time_payload,
    shift_month,
    time_caption,
    today_local,
)
from app.services.transport_service import format_trip_date, notify_operators_new_order
from app.texts import (
    TRANSPORT_ASK_DESTINATION,
    TRANSPORT_CANCELLED,
    TRANSPORT_CONFIRM,
    TRANSPORT_INVALID_DATE,
    TRANSPORT_INVALID_DESTINATION,
    TRANSPORT_INVALID_TIME,
    TRANSPORT_NEED_ROLE,
    TRANSPORT_NO_TIME_TODAY,
    TRANSPORT_SENT,
    TRANSPORT_START_HINT,
)

FLOW_NONE = "none"
FLOW_DATE = "date"
FLOW_TIME = "time"
FLOW_DESTINATION = "destination"
FLOW_CONFIRM = "confirm"


def in_transport_flow(user: User) -> bool:
    return (user.transport_flow_state or FLOW_NONE) != FLOW_NONE


def _clear_draft(user: User) -> None:
    user.transport_flow_state = FLOW_NONE
    user.transport_draft_date = None
    user.transport_draft_time = None
    user.transport_draft_destination = None


async def _reply_or_edit(
    user: User,
    text: str,
    attachments: list[dict],
    *,
    callback_id: str | None,
) -> None:
    if callback_id:
        await max_client.answer_callback(
            callback_id,
            message={"text": text, "format": "html", "attachments": attachments},
        )
        return
    await max_client.send_message(user.max_user_id, text, attachments=attachments, menu_bar=False)


async def _send_date_picker(
    user: User,
    *,
    year: int | None = None,
    month: int | None = None,
    callback_id: str | None = None,
    intro: str | None = None,
) -> None:
    today = today_local()
    year = year or today.year
    month = month or today.month
    text = calendar_caption(year, month)
    if intro:
        text = f"{intro}\n\n{text.split(chr(10) + chr(10), 1)[-1]}"
    await _reply_or_edit(
        user,
        text,
        build_calendar_keyboard(year, month),
        callback_id=callback_id,
    )


async def _send_time_picker(
    session: AsyncSession,
    user: User,
    trip_date_iso: str,
    *,
    callback_id: str | None = None,
) -> None:
    if not has_time_slots(trip_date_iso):
        user.transport_flow_state = FLOW_DATE
        user.transport_draft_date = None
        await session.commit()
        today = today_local()
        cal = calendar_caption(today.year, today.month)
        text = f"{TRANSPORT_NO_TIME_TODAY}\n\n{cal.split(chr(10) + chr(10), 1)[-1]}"
        await _reply_or_edit(
            user,
            text,
            build_calendar_keyboard(today.year, today.month),
            callback_id=callback_id,
        )
        return
    await _reply_or_edit(
        user,
        time_caption(trip_date_iso),
        build_time_keyboard(trip_date_iso),
        callback_id=callback_id,
    )


async def try_start_transport_order(session: AsyncSession, user: User) -> bool:
    if not user.is_registered:
        return False
    if not user_can_order_transport(user):
        await max_client.send_message(user.max_user_id, TRANSPORT_NEED_ROLE)
        return True

    from app.services.appeal_flow import APPEAL_FLOW_NONE
    from app.services.profile_flow import in_profile_flow
    from app.texts import PROFILE_FINISH_EDIT

    if in_profile_flow(user):
        from app.max_client import profile_cancel_keyboard

        await max_client.send_message(
            user.max_user_id,
            PROFILE_FINISH_EDIT,
            attachments=profile_cancel_keyboard(),
            menu_bar=False,
        )
        return True
    if (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_NONE:
        from app.max_client import appeal_cancel_keyboard

        await max_client.send_message(
            user.max_user_id,
            "Сначала завершите или отмените обращение.",
            attachments=appeal_cancel_keyboard(),
            menu_bar=False,
        )
        return True
    if in_transport_flow(user):
        if user.transport_flow_state == FLOW_DATE:
            await _send_date_picker(user)
        elif user.transport_flow_state == FLOW_TIME and user.transport_draft_date:
            await _send_time_picker(session, user, user.transport_draft_date)
        else:
            await max_client.send_message(
                user.max_user_id,
                "Вы уже оформляете заявку. Продолжите или отмените.",
                attachments=transport_cancel_keyboard(),
                menu_bar=False,
            )
        return True

    user.transport_flow_state = FLOW_DATE
    user.transport_draft_date = None
    user.transport_draft_time = None
    user.transport_draft_destination = None
    await session.commit()
    await _send_date_picker(user, intro=TRANSPORT_START_HINT)
    return True


async def try_handle_transport_callback(
    session: AsyncSession,
    user: User,
    payload: str,
    *,
    callback_id: str | None = None,
) -> bool:
    if payload == "transport_confirm":
        if user.transport_flow_state != FLOW_CONFIRM:
            return False
        if not all(
            [
                user.transport_draft_date,
                user.transport_draft_time,
                user.transport_draft_destination,
            ]
        ):
            await max_client.send_message(user.max_user_id, "Заявка неполная. Начните заново.")
            _clear_draft(user)
            await session.commit()
            return True

        order = TransportOrder(
            requester_id=user.id,
            trip_date=user.transport_draft_date,
            trip_time=user.transport_draft_time,
            destination=user.transport_draft_destination.strip(),
            status="new",
        )
        session.add(order)
        _clear_draft(user)
        await session.commit()
        await session.refresh(order)

        if callback_id:
            await max_client.answer_callback(callback_id, notification="Заявка отправлена")
        await max_client.send_message(
            user.max_user_id,
            TRANSPORT_SENT.format(order_id=order.id),
        )
        await notify_operators_new_order(session, order)
        from app.services.bot_menu import send_main_menu

        await send_main_menu(user.max_user_id, registered=True, user=user)
        return True

    if payload == "transport_cancel":
        if not in_transport_flow(user):
            return False
        _clear_draft(user)
        await session.commit()
        if callback_id:
            await max_client.answer_callback(callback_id, notification="Отменено")
        await max_client.send_message(user.max_user_id, TRANSPORT_CANCELLED)
        from app.services.bot_menu import send_main_menu

        await send_main_menu(user.max_user_id, registered=True, user=user)
        return True

    if payload == "tcal_noop":
        if callback_id:
            await max_client.answer_callback(callback_id)
        return True

    if payload.startswith(("tcal:", "tcal_p:", "tcal_n:")):
        if (user.transport_flow_state or FLOW_NONE) not in {FLOW_DATE, FLOW_TIME}:
            return False
        user.transport_flow_state = FLOW_DATE
        user.transport_draft_time = None
        await session.commit()
        parsed = parse_month_payload(payload)
        if not parsed:
            return False
        year, month = parsed
        if payload.startswith("tcal_p:"):
            year, month = shift_month(year, month, -1)
        elif payload.startswith("tcal_n:"):
            year, month = shift_month(year, month, 1)
        await _send_date_picker(user, year=year, month=month, callback_id=callback_id)
        return True

    if payload.startswith("tday:"):
        if (user.transport_flow_state or FLOW_NONE) != FLOW_DATE:
            return False
        trip_date = parse_day_payload(payload)
        if not trip_date:
            if callback_id:
                await max_client.answer_callback(callback_id, notification="Дата недоступна")
            return True
        user.transport_draft_date = trip_date
        user.transport_flow_state = FLOW_TIME
        await session.commit()
        await _send_time_picker(session, user, trip_date, callback_id=callback_id)
        return True

    if payload.startswith("ttime:"):
        if (user.transport_flow_state or FLOW_NONE) != FLOW_TIME or not user.transport_draft_date:
            return False
        trip_time = parse_time_payload(payload)
        if not trip_time:
            if callback_id:
                await max_client.answer_callback(callback_id, notification="Время недоступно")
            return True
        user.transport_draft_time = trip_time
        user.transport_flow_state = FLOW_DESTINATION
        await session.commit()
        if callback_id:
            await max_client.answer_callback(callback_id, notification="Принято")
        await max_client.send_message(
            user.max_user_id,
            f"<b>Заказ автомобиля</b>\n\n"
            f"Дата: <b>{format_trip_date(user.transport_draft_date)}</b>\n"
            f"Время: <b>{trip_time}</b>\n\n"
            f"{TRANSPORT_ASK_DESTINATION}",
            attachments=transport_cancel_keyboard(),
            menu_bar=False,
        )
        return True

    return False


async def handle_transport_flow_message(
    session: AsyncSession,
    user: User,
    text: str,
) -> bool:
    if not in_transport_flow(user):
        return False

    normalized = text.strip()
    if normalized.lower() in {"отмена", "cancel"}:
        _clear_draft(user)
        await session.commit()
        await max_client.send_message(user.max_user_id, TRANSPORT_CANCELLED)
        from app.services.bot_menu import send_main_menu

        await send_main_menu(user.max_user_id, registered=True, user=user)
        return True

    state = user.transport_flow_state or FLOW_NONE

    if state == FLOW_DATE:
        today = today_local()
        await max_client.send_message(
            user.max_user_id,
            TRANSPORT_INVALID_DATE,
            attachments=build_calendar_keyboard(today.year, today.month),
            menu_bar=False,
        )
        return True

    if state == FLOW_TIME:
        await max_client.send_message(
            user.max_user_id,
            TRANSPORT_INVALID_TIME,
            attachments=build_time_keyboard(user.transport_draft_date or today_local().isoformat()),
            menu_bar=False,
        )
        return True

    if state == FLOW_DESTINATION:
        if len(normalized) < 3:
            await max_client.send_message(
                user.max_user_id,
                TRANSPORT_INVALID_DESTINATION,
                attachments=transport_cancel_keyboard(),
                menu_bar=False,
            )
            return True
        user.transport_draft_destination = normalized[:500]
        user.transport_flow_state = FLOW_CONFIRM
        await session.commit()
        summary = (
            f"{TRANSPORT_CONFIRM}\n\n"
            f"Дата: <b>{format_trip_date(user.transport_draft_date or '')}</b>\n"
            f"Время: <b>{user.transport_draft_time}</b>\n"
            f"Направление: <b>{user.transport_draft_destination}</b>"
        )
        await max_client.send_message(
            user.max_user_id,
            summary,
            attachments=transport_confirm_keyboard(),
            menu_bar=False,
        )
        return True

    return False
