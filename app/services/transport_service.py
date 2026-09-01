from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.max_client import max_client
from app.models import Driver, TransportOrder, User, Vehicle
from app.services.audience_roles import user_is_transport_operator

DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_trip_date(text: str) -> str | None:
    match = DATE_RE.match(text.strip())
    if not match:
        return None
    day, month, year = match.groups()
    try:
        datetime(int(year), int(month), int(day))
    except ValueError:
        return None
    return f"{year}-{month}-{day}"


def format_trip_date(iso_date: str) -> str:
    try:
        year, month, day = iso_date.split("-")
        return f"{day}.{month}.{year}"
    except ValueError:
        return iso_date


def parse_trip_time(text: str) -> str | None:
    match = TIME_RE.match(text.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def format_order_summary(order: TransportOrder, *, requester: User | None = None) -> str:
    passenger = requester.fio if requester and requester.fio else "—"
    phone = requester.phone if requester and requester.phone else "—"
    return (
        f"<b>Заявка #{order.id}</b>\n"
        f"Пассажир: {passenger}\n"
        f"Телефон: {phone}\n"
        f"Дата: {format_trip_date(order.trip_date)}\n"
        f"Время: {order.trip_time}\n"
        f"Направление: {order.destination}"
    )


async def notify_operators_new_order(session: AsyncSession, order: TransportOrder) -> None:
    result = await session.execute(
        select(User).where(User.is_registered.is_(True), User.is_active.is_(True))
    )
    requester = await session.get(User, order.requester_id)
    summary = format_order_summary(order, requester=requester)
    text = (
        f"🚗 <b>Новая заявка на автомобиль</b>\n\n{summary}\n\n"
        "Откройте меню → «Заявки на авто», чтобы назначить машину и водителя."
    )
    for user in result.scalars().all():
        if user_is_transport_operator(user):
            try:
                await max_client.send_message(user.max_user_id, text)
            except Exception:
                pass


async def assign_transport_order(
    session: AsyncSession,
    *,
    order_id: int,
    vehicle_id: int,
    driver_id: int,
    operator: User,
) -> TransportOrder:
    order = await session.get(
        TransportOrder,
        order_id,
        options=(selectinload(TransportOrder.requester),),
    )
    if not order or order.status not in {"new", "assigned"}:
        raise ValueError("Заявка недоступна для назначения")

    vehicle = await session.get(Vehicle, vehicle_id)
    driver = await session.get(Driver, driver_id)
    if not vehicle or not vehicle.is_active:
        raise ValueError("Автомобиль не найден")
    if not driver or not driver.is_active:
        raise ValueError("Водитель не найден")

    order.vehicle_id = vehicle.id
    order.driver_id = driver.id
    order.operator_id = operator.id
    order.status = "assigned"
    order.assigned_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(order)

    requester = order.requester or await session.get(User, order.requester_id)
    if requester:
        text = (
            f"✅ <b>Вам назначен автомобиль</b>\n\n"
            f"{format_order_summary(order, requester=requester)}\n\n"
            f"<b>Авто:</b> {vehicle.make_model}, {vehicle.plate_number}\n"
            f"<b>Водитель:</b> {driver.fio}\n"
            f"<b>Телефон водителя:</b> {driver.phone}"
        )
        await max_client.send_message(requester.max_user_id, text)

    return order
