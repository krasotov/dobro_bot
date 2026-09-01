from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import max_client, transport_confirm_keyboard
from app.models import TransportOrder, User
from app.services.audience_roles import user_can_order_transport
from app.services.transport_service import (
    format_order_summary,
    format_trip_date,
    notify_operators_new_order,
    parse_trip_date,
    parse_trip_time,
)
from app.texts import (
    TRANSPORT_ASK_DATE,
    TRANSPORT_ASK_DESTINATION,
    TRANSPORT_ASK_TIME,
    TRANSPORT_CANCELLED,
    TRANSPORT_CONFIRM,
    TRANSPORT_INVALID_DATE,
    TRANSPORT_INVALID_DESTINATION,
    TRANSPORT_INVALID_TIME,
    TRANSPORT_NEED_ROLE,
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
        await max_client.send_message(user.max_user_id, PROFILE_FINISH_EDIT)
        return True
    if (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_NONE:
        await max_client.send_message(
            user.max_user_id,
            "Сначала завершите или отмените обращение (<b>отмена</b>).",
        )
        return True
    if in_transport_flow(user):
        await max_client.send_message(
            user.max_user_id,
            "Вы уже оформляете заявку. Продолжите ввод или напишите <b>отмена</b>.",
        )
        return True

    user.transport_flow_state = FLOW_DATE
    user.transport_draft_date = None
    user.transport_draft_time = None
    user.transport_draft_destination = None
    await session.commit()
    await max_client.send_message(user.max_user_id, TRANSPORT_START_HINT + "\n\n" + TRANSPORT_ASK_DATE)
    return True


async def try_handle_transport_callback(
    session: AsyncSession,
    user: User,
    payload: str,
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
        await max_client.send_message(user.max_user_id, TRANSPORT_CANCELLED)
        from app.services.bot_menu import send_main_menu

        await send_main_menu(user.max_user_id, registered=True, user=user)
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
        parsed = parse_trip_date(normalized)
        if not parsed:
            await max_client.send_message(user.max_user_id, TRANSPORT_INVALID_DATE)
            return True
        user.transport_draft_date = parsed
        user.transport_flow_state = FLOW_TIME
        await session.commit()
        await max_client.send_message(user.max_user_id, TRANSPORT_ASK_TIME)
        return True

    if state == FLOW_TIME:
        parsed = parse_trip_time(normalized)
        if not parsed:
            await max_client.send_message(user.max_user_id, TRANSPORT_INVALID_TIME)
            return True
        user.transport_draft_time = parsed
        user.transport_flow_state = FLOW_DESTINATION
        await session.commit()
        await max_client.send_message(user.max_user_id, TRANSPORT_ASK_DESTINATION)
        return True

    if state == FLOW_DESTINATION:
        if len(normalized) < 3:
            await max_client.send_message(user.max_user_id, TRANSPORT_INVALID_DESTINATION)
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
        )
        return True

    return False
