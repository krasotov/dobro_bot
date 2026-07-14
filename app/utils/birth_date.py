from __future__ import annotations

import re
from datetime import datetime, timezone

BIRTH_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def parse_birth_date(value: str) -> str | None:
    """Принимает только ДД.ММ.ГГГГ и возвращает дату в том же формате."""
    text = value.strip()
    match = BIRTH_DATE_RE.match(text)
    if not match:
        return None

    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3))

    if year < 1925:
        return None

    try:
        birth = datetime(year, month, day)
    except ValueError:
        return None

    today = datetime.now(timezone.utc).date()
    if birth.date() > today:
        return None

    age = today.year - birth.year - (
        (today.month, today.day) < (birth.month, birth.day)
    )
    if age < 14:
        return None

    return f"{day:02d}.{month:02d}.{year:04d}"
