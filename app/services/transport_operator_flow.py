from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.max_client import max_client
from app.models import Driver, TransportOrder, User, Vehicle
from app.services.audience_roles import user_is_transport_operator
from app.services.transport_service import assign_transport_order, format_order_summary
from app.texts import TRANSPORT_OPERATOR_NEED_ROLE, TRANSPORT_OPERATOR_NO_ORDERS


def _vehicle_keyboard(order_id: int, vehicles: list[Vehicle]) -> list[dict]:
    rows: list[list[dict]] = []
    row: list[dict] = []
    for vehicle in vehicles:
        label = f"{vehicle.make_model} ({vehicle.plate_number})"[:64]
        row.append(
            {
                "type": "callback",
                "text": label,
                "payload": f"transport_op_vehicle_{order_id}_{vehicle.id}",
            }
        )
        if len(row) == 1:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"type": "callback", "text": "← К заявкам", "payload": "menu_transport_ops"}])
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def _driver_keyboard(order_id: int, vehicle_id: int, drivers: list[Driver]) -> list[dict]:
    rows: list[list[dict]] = []
    for driver in drivers:
        label = f"{driver.fio}"[:64]
        rows.append(
            [
                {
                    "type": "callback",
                    "text": label,
                    "payload": f"transport_op_driver_{order_id}_{vehicle_id}_{driver.id}",
                }
            ]
        )
    rows.append([{"type": "callback", "text": "← К автомобилям", "payload": f"transport_op_order_{order_id}"}])
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def _orders_keyboard(orders: list[TransportOrder]) -> list[dict]:
    rows: list[list[dict]] = []
    for order in orders[:10]:
        rows.append(
            [
                {
                    "type": "callback",
                    "text": f"#{order.id} {order.trip_time} → {order.destination[:24]}",
                    "payload": f"transport_op_order_{order.id}",
                }
            ]
        )
    return [{"type": "inline_keyboard", "payload": {"buttons": rows or [[{"type": "callback", "text": "Обновить", "payload": "menu_transport_ops"}]]}}]


async def send_operator_orders_list(session: AsyncSession, user: User) -> None:
    if not user_is_transport_operator(user):
        await max_client.send_message(user.max_user_id, TRANSPORT_OPERATOR_NEED_ROLE)
        return

    result = await session.execute(
        select(TransportOrder)
        .where(TransportOrder.status == "new")
        .order_by(TransportOrder.created_at.asc())
        .limit(10)
    )
    orders = result.scalars().all()
    if not orders:
        await max_client.send_message(user.max_user_id, TRANSPORT_OPERATOR_NO_ORDERS)
        return

    await max_client.send_message(
        user.max_user_id,
        "<b>Новые заявки на автомобиль</b>\nВыберите заявку:",
        attachments=_orders_keyboard(orders),
    )


async def try_handle_transport_operator_callback(
    session: AsyncSession,
    user: User,
    payload: str,
) -> bool:
    if payload == "menu_transport_ops":
        await send_operator_orders_list(session, user)
        return True

    if payload.startswith("transport_op_order_"):
        if not user_is_transport_operator(user):
            await max_client.send_message(user.max_user_id, TRANSPORT_OPERATOR_NEED_ROLE)
            return True
        order_id = int(payload.removeprefix("transport_op_order_"))
        order = await session.get(
            TransportOrder,
            order_id,
            options=(selectinload(TransportOrder.requester),),
        )
        if not order or order.status != "new":
            await max_client.send_message(user.max_user_id, "Заявка уже обработана или не найдена.")
            await send_operator_orders_list(session, user)
            return True

        vehicles_result = await session.execute(
            select(Vehicle).where(Vehicle.is_active.is_(True)).order_by(Vehicle.make_model.asc())
        )
        vehicles = vehicles_result.scalars().all()
        if not vehicles:
            await max_client.send_message(
                user.max_user_id,
                "В базе нет активных автомобилей. Добавьте их в админке.",
            )
            return True

        summary = format_order_summary(order, requester=order.requester)
        await max_client.send_message(
            user.max_user_id,
            f"{summary}\n\n<b>Выберите автомобиль:</b>",
            attachments=_vehicle_keyboard(order.id, vehicles),
        )
        return True

    if payload.startswith("transport_op_vehicle_"):
        if not user_is_transport_operator(user):
            return True
        _, rest = payload.split("_vehicle_", 1)
        order_id_str, vehicle_id_str = rest.split("_", 1)
        order_id, vehicle_id = int(order_id_str), int(vehicle_id_str)

        vehicle = await session.get(Vehicle, vehicle_id)
        if not vehicle or not vehicle.is_active:
            await max_client.send_message(user.max_user_id, "Автомобиль недоступен.")
            return True

        drivers_result = await session.execute(
            select(Driver)
            .where(Driver.is_active.is_(True), Driver.vehicle_id == vehicle_id)
            .order_by(Driver.fio.asc())
        )
        drivers = drivers_result.scalars().all()
        if not drivers:
            drivers_result = await session.execute(
                select(Driver).where(Driver.is_active.is_(True)).order_by(Driver.fio.asc())
            )
            drivers = drivers_result.scalars().all()

        if not drivers:
            await max_client.send_message(
                user.max_user_id,
                "Нет активных водителей. Добавьте их в админке.",
            )
            return True

        await max_client.send_message(
            user.max_user_id,
            f"<b>{vehicle.make_model}</b> ({vehicle.plate_number})\n\nВыберите водителя:",
            attachments=_driver_keyboard(order_id, vehicle_id, drivers),
        )
        return True

    if payload.startswith("transport_op_driver_"):
        if not user_is_transport_operator(user):
            return True
        _, rest = payload.split("_driver_", 1)
        order_id_str, vehicle_id_str, driver_id_str = rest.split("_", 2)
        order_id, vehicle_id, driver_id = int(order_id_str), int(vehicle_id_str), int(driver_id_str)
        try:
            order = await assign_transport_order(
                session,
                order_id=order_id,
                vehicle_id=vehicle_id,
                driver_id=driver_id,
                operator=user,
            )
        except ValueError as exc:
            await max_client.send_message(user.max_user_id, str(exc))
            await send_operator_orders_list(session, user)
            return True

        vehicle = await session.get(Vehicle, vehicle_id)
        driver = await session.get(Driver, driver_id)
        await max_client.send_message(
            user.max_user_id,
            f"✅ Заявка #{order.id} назначена.\n"
            f"{vehicle.make_model if vehicle else '—'} · {driver.fio if driver else '—'}",
        )
        await send_operator_orders_list(session, user)
        return True

    return False
