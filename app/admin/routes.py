from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.auth import (
    ROLE_SUPERADMIN,
    admin_context,
    can_manage_broadcasts,
    require_auth,
    require_superadmin,
)
from app.database import async_session, get_session
from app.models import AdminRole, AdminUser, Broadcast, NewsChannelSource, Screenshot, User
from app.services.broadcast import encode_districts, send_broadcast
from app.services.broadcast_media import upload_broadcast_image, validate_broadcast_image
from app.services.channel_news import get_news_channel_source
from app.utils.password import hash_password, verify_password

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/admin/templates")


def _template_context(request: Request, **extra: object) -> dict[str, object]:
    return {"request": request, **admin_context(request), **extra}


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
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None},
    )


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    from app.config import settings

    username = username.strip()
    if username == settings.admin_username and password == settings.admin_password:
        request.session["admin"] = True
        request.session["admin_role"] = ROLE_SUPERADMIN
        request.session["admin_username"] = username
        request.session.pop("admin_id", None)
        return RedirectResponse("/admin/", status_code=303)

    result = await session.execute(
        select(AdminUser).where(
            AdminUser.username == username,
            AdminUser.is_active.is_(True),
        )
    )
    admin_user = result.scalar_one_or_none()
    if admin_user and verify_password(password, admin_user.password_hash):
        request.session["admin"] = True
        request.session["admin_role"] = admin_user.role.value
        request.session["admin_username"] = admin_user.username
        request.session["admin_id"] = admin_user.id
        return RedirectResponse("/admin/broadcasts", status_code=303)

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
    redirect = require_superadmin(request)
    if redirect:
        return redirect

    total = await session.scalar(select(func.count()).select_from(User))
    registered = await session.scalar(
        select(func.count()).select_from(User).where(User.is_registered.is_(True))
    )
    screenshots = await session.scalar(select(func.count()).select_from(Screenshot))
    broadcasts = await session.scalar(select(func.count()).select_from(Broadcast))
    staff_count = await session.scalar(select(func.count()).select_from(AdminUser))

    return templates.TemplateResponse(
        "dashboard.html",
        _template_context(
            request,
            total=total or 0,
            registered=registered or 0,
            screenshots=screenshots or 0,
            broadcasts=broadcasts or 0,
            staff_count=staff_count or 0,
        ),
    )


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    redirect = require_superadmin(request)
    if redirect:
        return redirect

    result = await session.execute(
        select(User)
        .options(selectinload(User.screenshots))
        .order_by(User.created_at.desc())
    )
    users = result.scalars().all()
    return templates.TemplateResponse(
        "users.html",
        _template_context(
            request,
            users=users,
            success=request.query_params.get("success"),
            error=request.query_params.get("error"),
        ),
    )


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    redirect = require_superadmin(request)
    if redirect:
        raise HTTPException(status_code=401)

    from app.services.user_admin import delete_user_record

    user = await delete_user_record(session, user_id)
    if not user:
        return RedirectResponse("/admin/users?error=Пользователь+не+найден", status_code=303)

    label = user.fio or user.max_user_id
    return RedirectResponse(
        f"/admin/users?success=Пользователь+удалён:+{label}.+Может+зарегистрироваться+заново+в+боте.",
        status_code=303,
    )


@router.get("/users/export")
async def export_users(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    redirect = require_superadmin(request)
    if redirect:
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
            "Дата рождения",
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
                user.birth_date or "",
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
    redirect = require_auth(request)
    if redirect:
        return redirect
    if not can_manage_broadcasts(request):
        return RedirectResponse("/admin/login", status_code=303)

    result = await session.execute(select(Broadcast).order_by(Broadcast.created_at.desc()))
    broadcasts = result.scalars().all()
    districts = await _get_districts(session)
    return templates.TemplateResponse(
        "broadcasts.html",
        _template_context(
            request,
            broadcasts=broadcasts,
            districts=districts,
            error=request.query_params.get("error"),
        ),
    )


@router.post("/broadcasts/create")
async def create_broadcast(
    request: Request,
    background_tasks: BackgroundTasks,
    text: str = Form(...),
    request_screenshot: bool = Form(False),
    districts: list[str] = Form(default=[]),
    image: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_session),
):
    redirect = require_auth(request)
    if redirect:
        raise HTTPException(status_code=401)
    if not can_manage_broadcasts(request):
        raise HTTPException(status_code=403)

    message_text = text.strip()
    if not message_text:
        return RedirectResponse("/admin/broadcasts?error=Введите+текст+сообщения", status_code=303)

    image_token = None
    image_filename = None
    if image and image.filename:
        content = await image.read()
        validation_error = validate_broadcast_image(image.filename, content)
        if validation_error:
            return RedirectResponse(
                f"/admin/broadcasts?error={validation_error.replace(' ', '+')}",
                status_code=303,
            )
        try:
            image_token = await upload_broadcast_image(content, image.filename)
            image_filename = image.filename
        except ValueError as exc:
            return RedirectResponse(
                f"/admin/broadcasts?error={str(exc).replace(' ', '+')[:200]}",
                status_code=303,
            )

    if request_screenshot:
        message_text += (
            "\n\nПосле выполнения действия, пожалуйста, пришлите скриншот, "
            "нажав на скрепку 📎 в этом чате."
        )

    broadcast = Broadcast(
        text=message_text,
        request_screenshot=request_screenshot,
        districts=encode_districts(districts),
        image_token=image_token,
        image_filename=image_filename,
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
    redirect = require_superadmin(request)
    if redirect:
        return redirect

    result = await session.execute(
        select(Screenshot)
        .options(selectinload(Screenshot.user))
        .order_by(Screenshot.created_at.desc())
    )
    screenshots = result.scalars().all()
    return templates.TemplateResponse(
        "screenshots.html",
        _template_context(request, screenshots=screenshots),
    )


@router.get("/staff", response_class=HTMLResponse)
async def staff_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    redirect = require_superadmin(request)
    if redirect:
        return redirect

    result = await session.execute(
        select(AdminUser).order_by(AdminUser.created_at.desc())
    )
    staff = result.scalars().all()
    return templates.TemplateResponse(
        "staff.html",
        _template_context(
            request,
            staff=staff,
            error=request.query_params.get("error"),
            success=request.query_params.get("success"),
        ),
    )


@router.post("/staff/create")
async def create_staff(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    redirect = require_superadmin(request)
    if redirect:
        raise HTTPException(status_code=401)

    username = username.strip().lower()
    display_name = display_name.strip() or None

    if len(username) < 3:
        return RedirectResponse("/admin/staff?error=Логин+должен+быть+не+короче+3+символов", status_code=303)
    if len(password) < 6:
        return RedirectResponse("/admin/staff?error=Пароль+должен+быть+не+короче+6+символов", status_code=303)

    from app.config import settings

    if username == settings.admin_username.lower():
        return RedirectResponse("/admin/staff?error=Этот+логин+зарезервирован", status_code=303)

    existing = await session.scalar(
        select(AdminUser.id).where(AdminUser.username == username)
    )
    if existing:
        return RedirectResponse("/admin/staff?error=Пользователь+с+таким+логином+уже+есть", status_code=303)

    admin_user = AdminUser(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name,
        role=AdminRole.BROADCASTER,
    )
    session.add(admin_user)
    await session.commit()
    return RedirectResponse("/admin/staff?success=Оператор+добавлен", status_code=303)


@router.post("/staff/{staff_id}/toggle")
async def toggle_staff(
    staff_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    redirect = require_superadmin(request)
    if redirect:
        raise HTTPException(status_code=401)

    admin_user = await session.get(AdminUser, staff_id)
    if not admin_user:
        raise HTTPException(status_code=404)

    admin_user.is_active = not admin_user.is_active
    await session.commit()
    status = "включён" if admin_user.is_active else "отключён"
    return RedirectResponse(f"/admin/staff?success=Оператор+{status}", status_code=303)


@router.get("/news-channel", response_class=HTMLResponse)
async def news_channel_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    redirect = require_superadmin(request)
    if redirect:
        return redirect

    source = await get_news_channel_source(session)
    if not source:
        source = NewsChannelSource(
            channel_slug="OErohanovaSP",
            channel_url="https://max.ru/OErohanovaSP",
            title="Канал главы Сергиево-Посадского ГО",
        )
        session.add(source)
        await session.commit()
        await session.refresh(source)

    result = await session.execute(
        select(Broadcast)
        .where(Broadcast.auto_from_channel.is_(True))
        .order_by(Broadcast.created_at.desc())
        .limit(20)
    )
    auto_broadcasts = result.scalars().all()
    return templates.TemplateResponse(
        "news_channel.html",
        _template_context(
            request,
            source=source,
            auto_broadcasts=auto_broadcasts,
            success=request.query_params.get("success"),
        ),
    )


@router.post("/news-channel/toggle")
async def toggle_news_channel(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    redirect = require_superadmin(request)
    if redirect:
        raise HTTPException(status_code=401)

    source = await get_news_channel_source(session)
    if not source:
        raise HTTPException(status_code=404)

    source.is_enabled = not source.is_enabled
    await session.commit()
    status = "включены" if source.is_enabled else "выключены"
    return RedirectResponse(f"/admin/news-channel?success=Автоновости+{status}", status_code=303)
