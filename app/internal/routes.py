from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session, get_session
from app.internal.auth import require_internal_api_token
from app.models import Broadcast, NewsChannelSource, Screenshot, User
from app.services.broadcast import encode_districts, send_broadcast
from app.services.broadcast_media import upload_broadcast_image, validate_broadcast_image
from app.services.channel_news import get_news_channel_source
from app.services.user_admin import delete_user_record

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
    source_post_id: str | None = Field(default=None, max_length=64)


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

    broadcast = Broadcast(
        text=body.text.strip(),
        districts=encode_districts(body.districts or []),
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
                "district": u.district,
                "phone": u.phone,
                "isRegistered": u.is_registered,
                "isActive": u.is_active,
                "createdAt": _iso(u.created_at),
            }
        )
    return {"users": users}


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
        items.append(
            {
                "id": b.id,
                "createdAt": _iso(b.created_at),
                "sentAt": _iso(b.sent_at),
                "textPreview": _text_preview(b.text),
                "districts": districts,
                "allDistricts": not districts,
                "hasImage": bool(b.image_token or b.image_url or b.image_filename),
                "autoFromChannel": b.auto_from_channel,
                "totalRecipients": b.total_recipients,
                "sentCount": b.sent_count,
                "failedCount": b.failed_count,
                "requestScreenshot": b.request_screenshot,
            }
        )
    return {"broadcasts": items}


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
