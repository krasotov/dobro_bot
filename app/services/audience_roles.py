from __future__ import annotations

import json
from typing import Any

from app.models import Broadcast, User

DEFAULT_AUDIENCE_ROLE = "resident"

AUDIENCE_ROLES: tuple[dict[str, str], ...] = (
    {"slug": "resident", "title": "Жители"},
    {"slug": "budget", "title": "Бюджетники"},
    {"slug": "deputy", "title": "Замы"},
    {"slug": "administration", "title": "Администрация"},
    {"slug": "driver", "title": "Водители"},
    {"slug": "garage_operator", "title": "Оператор гаража"},
    {"slug": "appeals_operator", "title": "Оператор обращений"},
    {"slug": "operator", "title": "Оператор (все функции)"},
)

ORDER_TRANSPORT_ROLES = frozenset({"deputy", "administration"})
GARAGE_OPERATOR_ROLES = frozenset({"garage_operator", "operator"})
APPEALS_OPERATOR_ROLES = frozenset({"appeals_operator", "operator"})

VALID_AUDIENCE_SLUGS = {item["slug"] for item in AUDIENCE_ROLES}


def audience_role_title(slug: str) -> str:
    for item in AUDIENCE_ROLES:
        if item["slug"] == slug:
            return item["title"]
    return slug


def encode_audience_roles(roles: list[str]) -> str | None:
    cleaned = [r.strip() for r in roles if r and r.strip() in VALID_AUDIENCE_SLUGS]
    if not cleaned:
        return None
    return json.dumps(sorted(set(cleaned)), ensure_ascii=False)


def parse_audience_roles(raw: str | None) -> list[str]:
    if not raw:
        return [DEFAULT_AUDIENCE_ROLE]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [DEFAULT_AUDIENCE_ROLE]
    if not isinstance(data, list):
        return [DEFAULT_AUDIENCE_ROLE]
    cleaned = [str(x) for x in data if str(x) in VALID_AUDIENCE_SLUGS]
    return cleaned or [DEFAULT_AUDIENCE_ROLE]


def ensure_default_audience_roles(user: User) -> None:
    if not user.audience_roles:
        user.audience_roles = encode_audience_roles([DEFAULT_AUDIENCE_ROLE])


def user_matches_broadcast_audience(user: User, broadcast_roles: list[str]) -> bool:
    if not broadcast_roles:
        return True
    user_roles = parse_audience_roles(user.audience_roles)
    return bool(set(user_roles) & set(broadcast_roles))


def get_broadcast_audience_role_list(broadcast: Broadcast) -> list[str]:
    if not broadcast.audience_roles:
        return []
    try:
        data = json.loads(broadcast.audience_roles)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if str(x) in VALID_AUDIENCE_SLUGS]


def user_has_audience_role(user: User, slug: str) -> bool:
    return slug in parse_audience_roles(user.audience_roles)


def user_can_order_transport(user: User) -> bool:
    roles = set(parse_audience_roles(user.audience_roles))
    return bool(roles & ORDER_TRANSPORT_ROLES)


def user_is_garage_operator(user: User) -> bool:
    roles = set(parse_audience_roles(user.audience_roles))
    return bool(roles & GARAGE_OPERATOR_ROLES)


def user_is_appeals_operator(user: User) -> bool:
    roles = set(parse_audience_roles(user.audience_roles))
    return bool(roles & APPEALS_OPERATOR_ROLES)


def user_is_transport_operator(user: User) -> bool:
    """Обратная совместимость."""
    return user_is_garage_operator(user)
