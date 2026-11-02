from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import async_session, get_session
from app.internal.auth import require_internal_api_token
from app.max_client import max_client
from app.models import (
    AdminRole,
    AdminUser,
    BotChat,
    Broadcast,
    Driver,
    NewsChannelSource,
    Screenshot,
    ServiceNote,
    TransportOrder,
    User,
    Vehicle,
)
from app.services.broadcast import encode_districts, encode_topics, send_broadcast
from app.services.audience_roles import (
    AUDIENCE_ROLES,
    encode_audience_roles,
    parse_audience_roles,
    user_is_appeals_operator,
)
from app.services.broadcast_media import upload_broadcast_image, validate_broadcast_image
from app.services.bot_chats import discover_chats_from_api, refresh_all_bot_chats, serialize_bot_chat
from app.services.outage_chat_ingest import get_outage_source_status, poll_outage_chat_messages
from app.services.channel_news import effective_source_chat_id, get_news_channel_source
from app.services.user_admin import delete_user_record
from app.services.service_note_service import parse_note_attachments
from app.services.transport_service import format_trip_date

SERVICE_NOTE_STATUS_LABELS = {
    "new": "Новая",
    "in_progress": "В работе",
    "awaiting_user": "Ожидает приёмки",
    "reopened": "Возвращена",
    "archived": "Архив",
}
from app.utils.password import hash_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal"])


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _text_preview(text: str, limit: int = 200) -> str:
    cleaned = text.replace("\r\n", "\n").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


async def _send_broadcast_bg(broadcast_id: int) -> None:
    async with async_session() as session:
        try:
            await send_broadcast(session, broadcast_id)
        except Exception:
            logger.exception("Background send_broadcast failed for id=%s", broadcast_id)


class CreateBroadcastBody(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    image_url: str | None = Field(default=None, max_length=2048)
    districts: list[str] | None = None
    topics: list[str] | None = None
    audienceRoles: list[str] | None = None
    source_post_id: str | None = Field(default=None, max_length=64)
    poll_id: str | None = Field(default=None, max_length=64)
    request_screenshot: bool = False


@router.post("/broadcasts")
async def create_broadcast(
    body: CreateBroadcastBody,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    image_token: str | None = None
    image_url: str | None = body.image_url
    image_filename: str | None = None

    if body.image_url:
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                response = await client.get(body.image_url)
            if response.status_code == 200 and response.content:
                path_part = body.image_url.split("/")[-1].split("?")[0] or "image.jpg"
                if "." not in path_part:
                    path_part = "image.jpg"
                err = validate_broadcast_image(path_part, response.content)
                if not err:
                    image_token = await upload_broadcast_image(response.content, path_part)
                    image_url = None
                    image_filename = path_part
        except Exception:
            logger.exception("Failed to mirror image for broadcast from %s", body.image_url[:120])

    message_text = body.text.strip()
    if body.request_screenshot:
        from app.texts import BROADCAST_SCREENSHOT_HINT

        message_text = f"{message_text.rstrip()}\n\n{BROADCAST_SCREENSHOT_HINT}"

    broadcast = Broadcast(
        text=message_text,
        districts=encode_districts(body.districts or []),
        topics=encode_topics(body.topics or []),
        audience_roles=encode_audience_roles(body.audienceRoles or []),
        poll_id=(body.poll_id or "").strip() or None,
        request_screenshot=body.request_screenshot,
        image_token=image_token,
        image_url=image_url,
        image_filename=image_filename,
        auto_from_channel=False,
    )
    session.add(broadcast)
    await session.commit()
    await session.refresh(broadcast)

    background_tasks.add_task(_send_broadcast_bg, broadcast.id)

    return {"broadcastId": broadcast.id, "status": "queued"}


@router.get("/districts")
async def list_districts(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(
        select(User.district)
        .where(User.district.is_not(None), User.district != "")
        .distinct()
        .order_by(User.district)
    )
    districts = [row[0] for row in result.all() if row[0]]
    return {"districts": districts}


@router.get("/users")
async def list_users(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    result = await session.execute(
        select(User).order_by(User.created_at.desc()).limit(limit)
    )
    users = []
    for u in result.scalars().all():
        users.append(
            {
                "id": u.id,
                "maxUserId": u.max_user_id,
                "fio": u.fio,
                "birthDate": u.birth_date,
                "address": u.address,
                "addressNormalized": u.address_normalized,
                "district": u.district,
                "phone": u.phone,
                "isRegistered": u.is_registered,
                "isActive": u.is_active,
                "audienceRoles": parse_audience_roles(u.audience_roles),
                "createdAt": _iso(u.created_at),
            }
        )
    return {"users": users}


@router.get("/users/export")
async def export_users(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    content, filename = await build_registered_users_csv(session)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class CreateStaffBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    displayName: str | None = Field(default=None, max_length=255)


class NotifyUserBody(BaseModel):
    maxUserId: int | str = Field(...)
    text: str = Field(min_length=1, max_length=4000)


class NotifyBatchBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    maxUserIds: list[int | str] = Field(min_length=1, max_length=500)


class NotifyOperatorsBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class UpdateUserAudienceRolesBody(BaseModel):
    audienceRoles: list[str] = Field(min_length=1, max_length=16)


@router.get("/audience-roles")
async def list_audience_roles(
    _: None = Depends(require_internal_api_token),
) -> dict:
    return {"roles": list(AUDIENCE_ROLES)}


@router.patch("/users/{user_id}/audience-roles")
async def update_user_audience_roles(
    user_id: int,
    body: UpdateUserAudienceRolesBody,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    encoded = encode_audience_roles(body.audienceRoles)
    if not encoded:
        raise HTTPException(status_code=400, detail="Укажите хотя бы одну допустимую группу")
    user.audience_roles = encoded
    await session.commit()
    return {"id": user.id, "audienceRoles": parse_audience_roles(user.audience_roles)}


@router.get("/users/registered")
async def list_registered_users(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=500),
) -> dict:
    query = (
        select(User)
        .where(User.is_registered.is_(True), User.is_active.is_(True), User.id > cursor)
        .order_by(User.id.asc())
        .limit(limit)
    )
    result = await session.execute(query)
    users = []
    next_cursor = cursor
    for u in result.scalars().all():
        next_cursor = u.id
        users.append(
            {
                "id": u.id,
                "maxUserId": u.max_user_id,
                "phone": u.phone,
                "addressStreet": u.address_street,
                "addressHouse": u.address_house,
                "addressApartment": u.address_apartment,
                "addressLatitude": u.address_latitude,
                "addressLongitude": u.address_longitude,
                "addressNormalized": u.address_normalized,
            }
        )
    return {"users": users, "nextCursor": next_cursor if users else None, "hasMore": len(users) >= limit}


@router.post("/users/notify")
async def notify_user(
    body: NotifyUserBody,
    _: None = Depends(require_internal_api_token),
) -> dict:
    max_user_id = int(body.maxUserId)
    await max_client.send_message(max_user_id, body.text.strip())
    return {"success": True}


@router.post("/users/notify-batch")
async def notify_users_batch(
    body: NotifyBatchBody,
    _: None = Depends(require_internal_api_token),
) -> dict:
    text = body.text.strip()
    sent = 0
    failed = 0
    for raw_id in body.maxUserIds:
        try:
            max_user_id = int(raw_id)
            await max_client.send_message(max_user_id, text)
            sent += 1
        except Exception:
            logger.exception("notify-batch failed for max_user_id=%s", raw_id)
            failed += 1
    return {"success": True, "sentCount": sent, "failedCount": failed}


@router.post("/operators/notify-appeals-operators")
async def notify_appeals_operators(
    body: NotifyOperatorsBody,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    text = body.text.strip()
    result = await session.execute(
        select(User).where(User.is_registered.is_(True), User.is_active.is_(True))
    )
    sent = 0
    failed = 0
    for user in result.scalars():
        if not user_is_appeals_operator(user):
            continue
        try:
            await max_client.send_message(user.max_user_id, text)
            sent += 1
        except Exception:
            logger.exception("notify appeals operator failed for max_user_id=%s", user.max_user_id)
            failed += 1
    return {"success": True, "sentCount": sent, "failedCount": failed}


@router.get("/staff")
async def list_staff(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(select(AdminUser).order_by(AdminUser.created_at.desc()))
    staff = []
    for member in result.scalars().all():
        staff.append(
            {
                "id": member.id,
                "username": member.username,
                "displayName": member.display_name,
                "role": member.role.value,
                "isActive": member.is_active,
                "createdAt": _iso(member.created_at),
            }
        )
    return {"staff": staff}


@router.post("/staff")
async def create_staff(
    body: CreateStaffBody,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    username = body.username.strip().lower()
    display_name = (body.displayName or "").strip() or None

    if username == settings.admin_username.lower():
        raise HTTPException(status_code=400, detail="Этот логин зарезервирован")

    existing = await session.scalar(select(AdminUser.id).where(AdminUser.username == username))
    if existing:
        raise HTTPException(status_code=409, detail="Пользователь с таким логином уже есть")

    admin_user = AdminUser(
        username=username,
        password_hash=hash_password(body.password),
        display_name=display_name,
        role=AdminRole.BROADCASTER,
    )
    session.add(admin_user)
    await session.commit()
    await session.refresh(admin_user)

    return {
        "success": True,
        "staff": {
            "id": admin_user.id,
            "username": admin_user.username,
            "displayName": admin_user.display_name,
            "role": admin_user.role.value,
            "isActive": admin_user.is_active,
            "createdAt": _iso(admin_user.created_at),
        },
    }


@router.post("/staff/{staff_id}/toggle")
async def toggle_staff(
    staff_id: int,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    admin_user = await session.get(AdminUser, staff_id)
    if not admin_user:
        raise HTTPException(status_code=404, detail="Operator not found")

    admin_user.is_active = not admin_user.is_active
    await session.commit()
    await session.refresh(admin_user)

    return {
        "success": True,
        "isActive": admin_user.is_active,
        "staffId": admin_user.id,
    }


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = await delete_user_record(session, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "success": True,
        "deletedUserId": user.id,
        "maxUserId": user.max_user_id,
    }


@router.get("/screenshots")
async def list_screenshots(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    result = await session.execute(
        select(Screenshot)
        .options(selectinload(Screenshot.user))
        .order_by(Screenshot.created_at.desc())
        .limit(limit)
    )
    items = []
    for shot in result.scalars().all():
        user = shot.user
        items.append(
            {
                "id": shot.id,
                "createdAt": _iso(shot.created_at),
                "fileUrl": shot.file_url,
                "hasFileToken": bool(shot.file_token),
                "broadcastId": shot.broadcast_id,
                "pollId": shot.poll_id,
                "userId": user.id if user else None,
                "maxUserId": user.max_user_id if user else None,
                "fio": user.fio if user else None,
            }
        )
    return {"screenshots": items}


@router.get("/summary")
async def summary(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    total_users = await session.scalar(select(func.count()).select_from(User)) or 0
    registered_users = (
        await session.scalar(
            select(func.count()).select_from(User).where(User.is_registered.is_(True))
        )
        or 0
    )
    screenshots = await session.scalar(select(func.count()).select_from(Screenshot)) or 0
    broadcasts = await session.scalar(select(func.count()).select_from(Broadcast)) or 0

    return {
        "usersTotal": total_users,
        "usersRegistered": registered_users,
        "screenshots": screenshots,
        "broadcasts": broadcasts,
    }


@router.get("/broadcasts")
async def list_broadcasts(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    result = await session.execute(
        select(Broadcast).order_by(Broadcast.created_at.desc()).limit(limit)
    )
    items = []
    for b in result.scalars().all():
        districts = b.get_district_list()
        topics = b.get_topic_list()
        audience_roles = b.get_audience_role_list()
        items.append(
            {
                "id": b.id,
                "createdAt": _iso(b.created_at),
                "sentAt": _iso(b.sent_at),
                "textPreview": _text_preview(b.text),
                "districts": districts,
                "topics": topics,
                "audienceRoles": audience_roles,
                "allDistricts": not districts,
                "allTopics": not topics,
                "allAudienceRoles": not audience_roles,
                "hasImage": bool(b.image_token or b.image_url or b.image_filename),
                "autoFromChannel": b.auto_from_channel,
                "totalRecipients": b.total_recipients,
                "sentCount": b.sent_count,
                "failedCount": b.failed_count,
                "requestScreenshot": b.request_screenshot,
                "pollId": b.poll_id,
            }
        )
    return {"broadcasts": items}


@router.get("/broadcasts/{broadcast_id}")
async def get_broadcast(
    broadcast_id: int,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    broadcast = await session.get(Broadcast, broadcast_id)
    if not broadcast:
        raise HTTPException(status_code=404, detail="Рассылка не найдена")

    return {
        "id": broadcast.id,
        "text": broadcast.text,
        "textPreview": _text_preview(broadcast.text),
        "autoFromChannel": broadcast.auto_from_channel,
        "imageUrl": broadcast.image_url,
        "hasImage": bool(broadcast.image_token or broadcast.image_url or broadcast.image_filename),
        "sourceMessageId": broadcast.source_message_id,
        "createdAt": _iso(broadcast.created_at),
        "sentCount": broadcast.sent_count,
        "totalRecipients": broadcast.total_recipients,
    }


@router.get("/news-channel")
async def news_channel(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    source = await get_news_channel_source(session)
    if not source:
        return {"configured": False}

    auto_result = await session.execute(
        select(Broadcast)
        .where(Broadcast.auto_from_channel.is_(True))
        .order_by(Broadcast.created_at.desc())
        .limit(15)
    )
    auto_broadcasts = []
    for b in auto_result.scalars().all():
        auto_broadcasts.append(
            {
                "id": b.id,
                "createdAt": _iso(b.created_at),
                "textPreview": _text_preview(b.text),
                "sentCount": b.sent_count,
                "totalRecipients": b.total_recipients,
                "failedCount": b.failed_count,
                "hasImage": bool(b.image_token or b.image_url or b.image_filename),
                "sourceMessageId": b.source_message_id,
            }
        )

    return {
        "configured": True,
        "channelSlug": source.channel_slug,
        "channelUrl": source.channel_url,
        "title": source.title,
        "chatId": source.chat_id,
        "isEnabled": source.is_enabled,
        "updatedAt": _iso(source.updated_at),
        "autoBroadcasts": auto_broadcasts,
    }


@router.post("/news-channel/toggle")
async def toggle_news_channel(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    source = await get_news_channel_source(session)
    if not source:
        source = NewsChannelSource(
            channel_slug="OErohanovaSP",
            channel_url="https://max.ru/OErohanovaSP",
            title="Канал главы Сергиево-Посадского ГО",
        )
        session.add(source)
        await session.flush()

    source.is_enabled = not source.is_enabled
    await session.commit()
    await session.refresh(source)

    return {
        "isEnabled": source.is_enabled,
        "chatId": source.chat_id,
        "channelUrl": source.channel_url,
    }


@router.get("/bot-chats")
async def list_bot_chats(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    source = await get_news_channel_source(session)
    news_chat_id = effective_source_chat_id(source)
    chats = (
        await session.scalars(select(BotChat).order_by(BotChat.added_at.desc()))
    ).all()
    active = [c for c in chats if (c.status or "active") == "active"]
    return {
        "chats": [serialize_bot_chat(chat, news_source_chat_id=news_chat_id) for chat in chats],
        "summary": {
            "total": len(chats),
            "active": len(active),
            "channels": sum(1 for c in active if c.is_channel or c.chat_type == "channel"),
            "groups": sum(1 for c in active if not c.is_channel and c.chat_type != "channel"),
        },
        "note": (
            "MAX API не отдаёт полный список чатов. Список пополняется при событиях "
            "bot_added / bot_removed и при привязке канала автоновостей."
        ),
    }


@router.post("/bot-chats/discover")
async def discover_bot_chats(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    discovered, total, error = await discover_chats_from_api(session)
    await session.commit()
    source = await get_news_channel_source(session)
    news_chat_id = effective_source_chat_id(source)
    chats = (
        await session.scalars(select(BotChat).order_by(BotChat.added_at.desc()))
    ).all()
    payload = {
        "discovered": discovered,
        "total": total,
        "error": error,
        "chats": [serialize_bot_chat(chat, news_source_chat_id=news_chat_id) for chat in chats],
        "summary": {
            "total": len(chats),
            "active": sum(1 for c in chats if (c.status or "active") == "active"),
            "channels": sum(
                1
                for c in chats
                if (c.status or "active") == "active" and (c.is_channel or c.chat_type == "channel")
            ),
            "groups": sum(
                1
                for c in chats
                if (c.status or "active") == "active" and not c.is_channel and c.chat_type != "channel"
            ),
        },
        "note": (
            "MAX API не отдаёт полный список чатов. Список пополняется при событиях "
            "bot_added / bot_removed и при привязке канала автоновостей."
        ),
    }
    if error and not chats:
        payload["warning"] = (
            "Сканирование через GET /chats недоступно. Добавьте бота в группу заново "
            "или дождитесь сообщения в чате — chat_id сохранится автоматически."
        )
    return payload


@router.post("/bot-chats/refresh")
async def refresh_bot_chats(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    ok_count, total = await refresh_all_bot_chats(session)
    await session.commit()
    source = await get_news_channel_source(session)
    news_chat_id = effective_source_chat_id(source)
    chats = (
        await session.scalars(select(BotChat).order_by(BotChat.added_at.desc()))
    ).all()
    return {
        "refreshed": ok_count,
        "total": total,
        "chats": [serialize_bot_chat(chat, news_source_chat_id=news_chat_id) for chat in chats],
        "summary": {
            "total": len(chats),
            "active": sum(1 for c in chats if (c.status or "active") == "active"),
            "channels": sum(
                1
                for c in chats
                if (c.status or "active") == "active" and (c.is_channel or c.chat_type == "channel")
            ),
            "groups": sum(
                1
                for c in chats
                if (c.status or "active") == "active" and not c.is_channel and c.chat_type != "channel"
            ),
        },
        "note": (
            "MAX API не отдаёт полный список чатов. Список пополняется при событиях "
            "bot_added / bot_removed и при привязке канала автоновостей."
        ),
    }


@router.get("/outage-source/status")
async def outage_source_status(_: None = Depends(require_internal_api_token)) -> dict:
    return await get_outage_source_status()


@router.post("/outage-source/poll")
async def outage_source_poll(_: None = Depends(require_internal_api_token)) -> dict:
    forwarded = await poll_outage_chat_messages()
    status = await get_outage_source_status()
    return {"forwarded": forwarded, "sourceStatus": status}


class VehicleBody(BaseModel):
    plateNumber: str = Field(min_length=3, max_length=32)
    makeModel: str = Field(min_length=2, max_length=255)
    isActive: bool = True


class DriverBody(BaseModel):
    fio: str = Field(min_length=2, max_length=255)
    phone: str = Field(min_length=5, max_length=32)
    vehicleId: int | None = None
    maxUserId: int | None = None
    isActive: bool = True


@router.get("/transport/vehicles")
async def list_vehicles(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(select(Vehicle).order_by(Vehicle.make_model.asc()))
    items = []
    for vehicle in result.scalars().all():
        items.append(
            {
                "id": vehicle.id,
                "plateNumber": vehicle.plate_number,
                "makeModel": vehicle.make_model,
                "isActive": vehicle.is_active,
            }
        )
    return {"vehicles": items}


@router.post("/transport/vehicles")
async def create_vehicle(
    body: VehicleBody,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    vehicle = Vehicle(
        plate_number=body.plateNumber.strip(),
        make_model=body.makeModel.strip(),
        is_active=body.isActive,
    )
    session.add(vehicle)
    await session.commit()
    await session.refresh(vehicle)
    return {
        "id": vehicle.id,
        "plateNumber": vehicle.plate_number,
        "makeModel": vehicle.make_model,
        "isActive": vehicle.is_active,
    }


@router.patch("/transport/vehicles/{vehicle_id}")
async def update_vehicle(
    vehicle_id: int,
    body: VehicleBody,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    vehicle = await session.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Автомобиль не найден")
    vehicle.plate_number = body.plateNumber.strip()
    vehicle.make_model = body.makeModel.strip()
    vehicle.is_active = body.isActive
    await session.commit()
    return {
        "id": vehicle.id,
        "plateNumber": vehicle.plate_number,
        "makeModel": vehicle.make_model,
        "isActive": vehicle.is_active,
    }


@router.get("/transport/drivers")
async def list_drivers(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(select(Driver).order_by(Driver.fio.asc()))
    items = []
    for driver in result.scalars().all():
        items.append(
            {
                "id": driver.id,
                "fio": driver.fio,
                "phone": driver.phone,
                "vehicleId": driver.vehicle_id,
                "maxUserId": driver.max_user_id,
                "isActive": driver.is_active,
            }
        )
    return {"drivers": items}


@router.post("/transport/drivers")
async def create_driver(
    body: DriverBody,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    driver = Driver(
        fio=body.fio.strip(),
        phone=body.phone.strip(),
        vehicle_id=body.vehicleId,
        max_user_id=body.maxUserId,
        is_active=body.isActive,
    )
    session.add(driver)
    await session.commit()
    await session.refresh(driver)
    return {
        "id": driver.id,
        "fio": driver.fio,
        "phone": driver.phone,
        "vehicleId": driver.vehicle_id,
        "maxUserId": driver.max_user_id,
        "isActive": driver.is_active,
    }


@router.patch("/transport/drivers/{driver_id}")
async def update_driver(
    driver_id: int,
    body: DriverBody,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    driver = await session.get(Driver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Водитель не найден")
    driver.fio = body.fio.strip()
    driver.phone = body.phone.strip()
    driver.vehicle_id = body.vehicleId
    driver.max_user_id = body.maxUserId
    driver.is_active = body.isActive
    await session.commit()
    return {
        "id": driver.id,
        "fio": driver.fio,
        "phone": driver.phone,
        "vehicleId": driver.vehicle_id,
        "maxUserId": driver.max_user_id,
        "isActive": driver.is_active,
    }


@router.get("/transport/orders")
async def list_transport_orders(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    query = select(TransportOrder).order_by(TransportOrder.created_at.desc()).limit(limit)
    if status:
        query = query.where(TransportOrder.status == status)
    result = await session.execute(query)
    items = []
    for order in result.scalars().all():
        requester = await session.get(User, order.requester_id)
        vehicle = await session.get(Vehicle, order.vehicle_id) if order.vehicle_id else None
        driver = await session.get(Driver, order.driver_id) if order.driver_id else None
        items.append(
            {
                "id": order.id,
                "status": order.status,
                "tripDate": format_trip_date(order.trip_date),
                "tripTime": order.trip_time,
                "destination": order.destination,
                "requesterFio": requester.fio if requester else None,
                "requesterPhone": requester.phone if requester else None,
                "vehiclePlate": vehicle.plate_number if vehicle else None,
                "vehicleMakeModel": vehicle.make_model if vehicle else None,
                "driverFio": driver.fio if driver else None,
                "driverPhone": driver.phone if driver else None,
                "createdAt": _iso(order.created_at),
                "assignedAt": _iso(order.assigned_at),
            }
        )
    return {"orders": items}


@router.get("/service-notes")
async def list_service_notes(
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    query = select(ServiceNote).order_by(ServiceNote.created_at.desc()).limit(limit)
    if status:
        query = query.where(ServiceNote.status == status)
    result = await session.execute(query)
    items = []
    for note in result.scalars().all():
        requester = await session.get(User, note.requester_id)
        operator = await session.get(User, note.operator_id) if note.operator_id else None
        attachment_count = len(parse_note_attachments(note.attachments))
        items.append(
            {
                "id": note.id,
                "status": note.status,
                "statusLabel": SERVICE_NOTE_STATUS_LABELS.get(note.status, note.status),
                "department": note.department,
                "roomNumber": note.room_number,
                "message": note.message,
                "attachmentCount": attachment_count,
                "requesterFio": requester.fio if requester else None,
                "requesterPhone": requester.phone if requester else None,
                "operatorFio": operator.fio if operator else None,
                "resolutionComment": note.resolution_comment,
                "createdAt": _iso(note.created_at),
                "assignedAt": _iso(note.assigned_at),
                "resolvedAt": _iso(note.resolved_at),
                "archivedAt": _iso(note.archived_at),
            }
        )
    return {"notes": items}


class ServiceNoteDeliveryFile(BaseModel):
    fileName: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=8, max_length=2000)


class ServiceNoteExternalSyncBody(BaseModel):
    status: str = Field(min_length=3, max_length=24)
    resolutionComment: str | None = Field(default=None, max_length=4000)
    operatorFio: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, max_length=32)
    deliveryFiles: list[ServiceNoteDeliveryFile] | None = None


@router.patch("/service-notes/{note_id}/external-sync")
async def external_sync_service_note(
    note_id: int,
    body: ServiceNoteExternalSyncBody,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.services.service_note_external_sync import apply_service_note_external_sync

    try:
        note = await apply_service_note_external_sync(
            session,
            note_id=note_id,
            status=body.status,
            resolution_comment=body.resolutionComment,
            operator_fio=body.operatorFio,
            delivery_files=[f.model_dump() for f in (body.deliveryFiles or [])],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"ok": True, "noteId": note.id, "status": note.status}


@router.delete("/service-notes/{note_id}")
async def delete_service_note(
    note_id: int,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    note = await session.get(ServiceNote, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    await session.delete(note)
    await session.commit()
    return {"success": True, "deletedNoteId": note_id}


@router.delete("/transport/orders/{order_id}")
async def delete_transport_order(
    order_id: int,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    order = await session.get(TransportOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    await session.delete(order)
    await session.commit()
    return {"success": True, "deletedOrderId": order_id}


@router.delete("/transport/vehicles/{vehicle_id}")
async def delete_vehicle(
    vehicle_id: int,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    vehicle = await session.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Автомобиль не найден")

    linked_orders = await session.scalar(
        select(func.count()).select_from(TransportOrder).where(TransportOrder.vehicle_id == vehicle_id)
    )
    if linked_orders:
        raise HTTPException(
            status_code=400,
            detail="Нельзя удалить: автомобиль указан в заявках. Отключите его или удалите заявки.",
        )

    drivers_result = await session.execute(select(Driver).where(Driver.vehicle_id == vehicle_id))
    for driver in drivers_result.scalars().all():
        driver.vehicle_id = None

    await session.delete(vehicle)
    await session.commit()
    return {"success": True, "deletedVehicleId": vehicle_id}


@router.delete("/transport/drivers/{driver_id}")
async def delete_driver(
    driver_id: int,
    _: None = Depends(require_internal_api_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    driver = await session.get(Driver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Водитель не найден")

    linked_orders = await session.scalar(
        select(func.count()).select_from(TransportOrder).where(TransportOrder.driver_id == driver_id)
    )
    if linked_orders:
        raise HTTPException(
            status_code=400,
            detail="Нельзя удалить: водитель указан в заявках. Отключите его или удалите заявки.",
        )

    await session.delete(driver)
    await session.commit()
    return {"success": True, "deletedDriverId": driver_id}
