from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.internal.auth import require_internal_api_token
from app.models import Broadcast, Screenshot, User
from app.services.channel_news import get_news_channel_source

router = APIRouter(prefix="/internal/v1", tags=["internal"])


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _text_preview(text: str, limit: int = 200) -> str:
    cleaned = text.replace("\r\n", "\n").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


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
    return {
        "configured": True,
        "channelSlug": source.channel_slug,
        "channelUrl": source.channel_url,
        "title": source.title,
        "chatId": source.chat_id,
        "isEnabled": source.is_enabled,
        "updatedAt": _iso(source.updated_at),
    }
