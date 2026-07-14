from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session, get_session
from app.models import Broadcast, Screenshot, User
from app.services.broadcast import encode_districts, send_broadcast

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/admin/templates")


def _is_authenticated(request: Request) -> bool:
    return bool(request.session.get("admin"))


async def _get_districts(session: AsyncSession) -> list[str]:
    result = await session.execute(
        select(User.district)
        .where(User.district.is_not(None), User.district != "")
        .distinct()
        .order_by(User.district)
    )
    return [row[0] for row in result.all() if row[0]]


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    from app.config import settings

    if username == settings.admin_username and password == settings.admin_password:
        request.session["admin"] = True
        return RedirectResponse("/admin/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Неверный логин или пароль"},
        status_code=401,
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=303)

    total = await session.scalar(select(func.count()).select_from(User))
    registered = await session.scalar(
        select(func.count()).select_from(User).where(User.is_registered.is_(True))
    )
    screenshots = await session.scalar(select(func.count()).select_from(Screenshot))
    broadcasts = await session.scalar(select(func.count()).select_from(Broadcast))

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "total": total or 0,
            "registered": registered or 0,
            "screenshots": screenshots or 0,
            "broadcasts": broadcasts or 0,
        },
    )


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=303)

    result = await session.execute(
        select(User)
        .options(selectinload(User.screenshots))
        .order_by(User.created_at.desc())
    )
    users = result.scalars().all()
    return templates.TemplateResponse("users.html", {"request": request, "users": users})


@router.get("/users/export")
async def export_users(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401)

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
            "Адрес",
            "Адрес (как ввёл пользователь)",
            "Район",
            "Телефон",
            "Телефон подтверждён",
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
                user.address_normalized or user.address or "",
                user.address or "",
                user.district or "",
                user.phone or "",
                "да" if user.phone_verified else "нет",
                user.registered_at.isoformat() if user.registered_at else "",
                len(user.screenshots),
                "да" if user.is_active else "нет",
            ]
        )

    output.seek(0)
    filename = f"users_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/broadcasts", response_class=HTMLResponse)
async def broadcasts_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=303)

    result = await session.execute(select(Broadcast).order_by(Broadcast.created_at.desc()))
    broadcasts = result.scalars().all()
    districts = await _get_districts(session)
    return templates.TemplateResponse(
        "broadcasts.html",
        {"request": request, "broadcasts": broadcasts, "districts": districts},
    )


@router.post("/broadcasts/create")
async def create_broadcast(
    request: Request,
    background_tasks: BackgroundTasks,
    text: str = Form(...),
    request_screenshot: bool = Form(False),
    districts: list[str] = Form(default=[]),
    session: AsyncSession = Depends(get_session),
):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401)

    message_text = text.strip()
    if request_screenshot:
        message_text += (
            "\n\nПосле выполнения действия, пожалуйста, пришлите скриншот, "
            "нажав на скрепку 📎 в этом чате."
        )

    broadcast = Broadcast(
        text=message_text,
        request_screenshot=request_screenshot,
        districts=encode_districts(districts),
    )
    session.add(broadcast)
    await session.commit()
    await session.refresh(broadcast)

    async def _run_broadcast(broadcast_id: int) -> None:
        async with async_session() as bg_session:
            await send_broadcast(bg_session, broadcast_id)

    background_tasks.add_task(_run_broadcast, broadcast.id)
    return RedirectResponse("/admin/broadcasts?started=1", status_code=303)


@router.get("/screenshots", response_class=HTMLResponse)
async def screenshots_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=303)

    result = await session.execute(
        select(Screenshot)
        .options(selectinload(Screenshot.user))
        .order_by(Screenshot.created_at.desc())
    )
    screenshots = result.scalars().all()
    return templates.TemplateResponse(
        "screenshots.html", {"request": request, "screenshots": screenshots}
    )
