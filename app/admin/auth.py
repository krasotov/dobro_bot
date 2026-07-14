from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse

ROLE_SUPERADMIN = "superadmin"
ROLE_BROADCASTER = "broadcaster"


def admin_context(request: Request) -> dict[str, object]:
    role = request.session.get("admin_role", "")
    return {
        "admin_role": role,
        "admin_username": request.session.get("admin_username", ""),
        "is_superadmin": role == ROLE_SUPERADMIN,
    }


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("admin"))


def require_auth(request: Request) -> RedirectResponse | None:
    if not is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


def require_superadmin(request: Request) -> RedirectResponse | None:
    redirect = require_auth(request)
    if redirect:
        return redirect
    if request.session.get("admin_role") != ROLE_SUPERADMIN:
        return RedirectResponse("/admin/broadcasts", status_code=303)
    return None


def can_manage_broadcasts(request: Request) -> bool:
    return request.session.get("admin_role") in {ROLE_SUPERADMIN, ROLE_BROADCASTER}
