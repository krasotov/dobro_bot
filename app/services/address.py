from __future__ import annotations

import asyncio
import logging

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.services.dadata import UNKNOWN_DISTRICT, NormalizedAddress, normalize_address

logger = logging.getLogger(__name__)


def apply_normalized_address(user: User, normalized: NormalizedAddress) -> None:
    user.address = normalized.raw
    user.address_normalized = normalized.normalized
    user.district = normalized.district
    user.region = normalized.region
    user.city = normalized.city
    user.settlement = normalized.settlement
    user.address_qc = normalized.quality_code


async def apply_address_to_user(user: User, raw_address: str) -> NormalizedAddress:
    normalized = await normalize_address(raw_address)
    apply_normalized_address(user, normalized)
    return normalized


async def reprocess_existing_addresses(session: AsyncSession) -> int:
    """Нормализует адреса пользователей, у которых ещё нет обработанного адреса."""
    await session.execute(
        text(
            "UPDATE users SET state = 'AWAITING_PHONE' "
            "WHERE state::text IN ('awaiting_address_confirm', 'AWAITING_ADDRESS_CONFIRM')"
        )
    )
    await session.commit()

    result = await session.execute(
        select(User).where(
            User.address.is_not(None),
            User.address != "",
            or_(
                User.address_normalized.is_(None),
                User.address_normalized == "",
                User.district.is_(None),
                User.district == "",
                User.district == UNKNOWN_DISTRICT,
            ),
        )
    )
    users = result.scalars().all()
    updated = 0

    for user in users:
        try:
            normalized = await normalize_address(user.address or "")
            apply_normalized_address(user, normalized)
            updated += 1
            await session.commit()
            await asyncio.sleep(0.15)
        except Exception:
            await session.rollback()
            logger.exception("Failed to normalize address for user %s", user.id)

    logger.info("Reprocessed %s addresses via DaData", updated)
    return updated
