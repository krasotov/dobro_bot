from __future__ import annotations

import csv
import io
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import User
from app.services.audience_roles import audience_role_title, parse_audience_roles


async def build_registered_users_csv(session: AsyncSession) -> tuple[bytes, str]:
    result = await session.execute(
        select(User)
        .options(selectinload(User.screenshots))
        .where(User.is_registered.is_(True))
        .order_by(User.registered_at.desc())
    )
    users = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            "ID",
            "MAX user_id",
            "ФИО",
            "Дата рождения",
            "Адрес",
            "Адрес (как ввёл пользователь)",
            "Район",
            "Телефон",
            "Телефон подтверждён",
            "Группы",
            "Дата регистрации",
            "Скриншотов",
            "Активен",
        ]
    )
    for user in users:
        writer.writerow(
            [
                user.id,
                user.max_user_id,
                user.fio or "",
                user.birth_date or "",
                user.address_normalized or user.address or "",
                user.address or "",
                user.district or "",
                user.phone or "",
                "да" if user.phone_verified else "нет",
                ", ".join(audience_role_title(s) for s in parse_audience_roles(user.audience_roles)),
                user.registered_at.isoformat() if user.registered_at else "",
                len(user.screenshots),
                "да" if user.is_active else "нет",
            ]
        )

    output.seek(0)
    filename = f"users_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return output.getvalue().encode("utf-8-sig"), filename
