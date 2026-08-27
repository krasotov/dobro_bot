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
from app.models import AdminRole, AdminUser, Broadcast, NewsChannelSource, Screenshot, User
from app.services.broadcast import encode_districts, encode_topics, send_broadcast
from app.services.audience_roles import (
    AUDIENCE_ROLES,
    encode_audience_roles,
    parse_audience_roles,
)
from app.services.broadcast_media import upload_broadcast_image, validate_broadcast_image
from app.services.channel_news import get_news_channel_source
from app.services.user_admin import delete_user_record
from app.services.user_export import build_registered_users_csv
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


class UpdateUserAudienceRolesBody(BaseModel):
    audienceRoles: list[str] = Field(min_length=1, max_length=8)


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
