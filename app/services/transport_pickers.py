from __future__ import annotations

from calendar import monthcalendar
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.max_client import message_button

TZ = ZoneInfo("Europe/Moscow")
MAX_MONTHS_AHEAD = 3
TIME_START_HOUR = 7
TIME_END_HOUR = 18

MONTH_NAMES = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)

WEEKDAY_HEADERS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def today_local() -> date:
    return datetime.now(TZ).date()


def max_booking_date() -> date:
    return today_local() + timedelta(days=MAX_MONTHS_AHEAD * 31)


def calendar_caption(year: int, month: int) -> str:
    return f"<b>Заказ автомобиля</b>\n\nВыберите <b>дату поездки</b> — {MONTH_NAMES[month]} {year}:"


def time_caption(trip_date_iso: str) -> str:
    from app.services.transport_service import format_trip_date

    return (
        f"<b>Заказ автомобиля</b>\n\n"
        f"Дата: <b>{format_trip_date(trip_date_iso)}</b>\n\n"
        "Выберите <b>время выезда</b>:"
    )


def _button(text: str, payload: str) -> dict[str, str]:
    return {"type": "callback", "text": text[:64], "payload": payload}


def _noop_button(text: str = "·") -> dict[str, str]:
    return _button(text, "tcal_noop")


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return first, last


def can_show_month(year: int, month: int) -> bool:
    first, last = _month_bounds(year, month)
    today = today_local()
    max_day = max_booking_date()
    return last >= today and first <= max_day


def clamp_month(year: int, month: int) -> tuple[int, int]:
    today = today_local()
    max_day = max_booking_date()
    current = date(year, month, 1)
    min_month = date(today.year, today.month, 1)
    max_month = date(max_day.year, max_day.month, 1)
    if current < min_month:
        return today.year, today.month
    if current > max_month:
        return max_day.year, max_day.month
    return year, month


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    month += delta
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return clamp_month(year, month)


def parse_month_payload(payload: str) -> tuple[int, int] | None:
    raw = payload.removeprefix("tcal:").removeprefix("tcal_p:").removeprefix("tcal_n:")
    if len(raw) != 6 or not raw.isdigit():
        return None
    year, month = int(raw[:4]), int(raw[4:6])
    if month < 1 or month > 12:
        return None
    return clamp_month(year, month)


def parse_day_payload(payload: str) -> str | None:
    raw = payload.removeprefix("tday:")
    if len(raw) != 8 or not raw.isdigit():
        return None
    year, month, day = int(raw[:4]), int(raw[4:6]), int(raw[6:8])
    try:
        selected = date(year, month, day)
    except ValueError:
        return None
    today = today_local()
    if selected < today or selected > max_booking_date():
        return None
    return selected.isoformat()


def parse_time_payload(payload: str) -> str | None:
    raw = payload.removeprefix("ttime:")
    if len(raw) != 4 or not raw.isdigit():
        return None
    hour, minute = int(raw[:2]), int(raw[2:])
    if hour < TIME_START_HOUR or hour > TIME_END_HOUR:
        return None
    if minute not in {0, 30}:
        return None
    if hour == TIME_END_HOUR and minute > 0:
        return None
    return f"{hour:02d}:{minute:02d}"


def is_time_available(trip_date_iso: str, hour: int, minute: int) -> bool:
    try:
        selected = date.fromisoformat(trip_date_iso)
    except ValueError:
        return False
    today = today_local()
    if selected > today:
        return True
    if selected < today:
        return False
    now = datetime.now(TZ)
    slot = datetime(now.year, now.month, now.day, hour, minute, tzinfo=TZ)
    return slot > now


def build_calendar_keyboard(year: int, month: int) -> list[dict[str, Any]]:
    year, month = clamp_month(year, month)
    today = today_local()
    max_day = max_booking_date()
    rows: list[list[dict[str, str]]] = []

    nav_row: list[dict[str, str]] = []
    prev_year, prev_month = shift_month(year, month, -1)
    next_year, next_month = shift_month(year, month, 1)
    if can_show_month(prev_year, prev_month):
        nav_row.append(_button("‹", f"tcal_p:{prev_year}{prev_month:02d}"))
    else:
        nav_row.append(_noop_button(" "))
    if can_show_month(next_year, next_month):
        nav_row.append(_button("›", f"tcal_n:{next_year}{next_month:02d}"))
    else:
        nav_row.append(_noop_button(" "))
    rows.append(nav_row)

    rows.append([_button(label, "tcal_noop") for label in WEEKDAY_HEADERS])

    for week in monthcalendar(year, month):
        row: list[dict[str, str]] = []
        for day in week:
            if day == 0:
                row.append(_noop_button())
                continue
            current = date(year, month, day)
            if current < today or current > max_day:
                row.append(_noop_button(str(day)))
                continue
            row.append(_button(str(day), f"tday:{year}{month:02d}{day:02d}"))
        rows.append(row)

    rows.append([_button("Отмена", "transport_cancel"), message_button("отмена")])
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def has_time_slots(trip_date_iso: str) -> bool:
    for hour in range(TIME_START_HOUR, TIME_END_HOUR + 1):
        for minute in (0, 30):
            if hour == TIME_END_HOUR and minute > 0:
                continue
            if is_time_available(trip_date_iso, hour, minute):
                return True
    return False


def build_time_keyboard(trip_date_iso: str) -> list[dict[str, Any]]:
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for hour in range(TIME_START_HOUR, TIME_END_HOUR + 1):
        for minute in (0, 30):
            if hour == TIME_END_HOUR and minute > 0:
                continue
            if not is_time_available(trip_date_iso, hour, minute):
                continue
            label = f"{hour:02d}:{minute:02d}"
            row.append(_button(label, f"ttime:{hour:02d}{minute:02d}"))
            if len(row) == 3:
                rows.append(row)
                row = []
    if row:
        rows.append(row)
    month_key = trip_date_iso.replace("-", "")[:6]
    rows.append(
        [
            _button("← Дата", f"tcal:{month_key}"),
            _button("Отмена", "transport_cancel"),
            message_button("отмена"),
        ]
    )
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]
