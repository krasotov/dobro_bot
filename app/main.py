from contextlib import asynccontextmanager
import logging
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app.admin.routes import router as admin_router
from app.internal.routes import router as internal_router
from app.config import settings
from app.database import get_session, init_db
from app.handlers.webhook import process_update
from app.max_client import max_client, setup_bot_commands

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    async def _reprocess_addresses() -> None:
        from app.database import async_session
        from app.services.address import reprocess_existing_addresses

        try:
            async with async_session() as session:
                count = await reprocess_existing_addresses(session)
                logger.info("Startup address reprocessing finished: %s users", count)
        except Exception:
            logger.exception("Startup address reprocessing failed")

    import asyncio

    asyncio.create_task(_reprocess_addresses())

    async def _poll_outage_chat_loop() -> None:
        await asyncio.sleep(30)
        while True:
            try:
                from app.services.outage_chat_ingest import poll_outage_chat_messages

                await poll_outage_chat_messages()
            except Exception:
                logger.exception("Outage chat poll loop failed")
            await asyncio.sleep(120)

    async def _poll_social_monitor_loop() -> None:
        await asyncio.sleep(45)
        while True:
            try:
                from app.services.social_monitor_ingest import poll_social_monitor_messages

                await poll_social_monitor_messages()
            except Exception:
                logger.exception("Social monitor poll loop failed")
            await asyncio.sleep(300)

    _admspgo_api = (settings.admspgo_api_url or "").strip()
    _admspgo_token = (settings.admspgo_internal_token or "").strip()
    if _admspgo_api and _admspgo_token:
        asyncio.create_task(_poll_outage_chat_loop())
        asyncio.create_task(_poll_social_monitor_loop())

    async def _poll_dialog_inboxes_loop() -> None:
        await asyncio.sleep(3)
        from app.database import async_session
        from app.services.dialog_inbox_poll import poll_operator_dialog_inboxes

        interval = max(2.0, float(getattr(settings, "dialog_inbox_poll_interval_sec", 4.0)))
        while True:
            try:
                async with async_session() as session:
                    await poll_operator_dialog_inboxes(session)
            except Exception:
                logger.exception("Dialog inbox poll loop failed")
            await asyncio.sleep(interval)

    if getattr(settings, "dialog_inbox_poll_enabled", True):
        asyncio.create_task(_poll_dialog_inboxes_loop())

    async def _poll_max_updates_loop() -> None:
        await asyncio.sleep(2)
        from app.services.max_updates_poll import poll_max_updates_once

        while True:
            try:
                await poll_max_updates_once()
            except Exception:
                logger.exception("MAX /updates poll loop failed")
                await asyncio.sleep(3)

    if getattr(settings, "max_updates_poll_enabled", True):
        asyncio.create_task(_poll_max_updates_loop())

    try:
        await setup_bot_commands()
        logger.info("MAX bot commands registered")
    except Exception:
        logger.exception("Failed to register MAX bot commands")

    if settings.webhook_url and settings.max_webhook_secret:
        try:
            await max_client.subscribe_webhook(
                settings.webhook_url,
                secret=settings.max_webhook_secret,
            )
            logger.info("MAX webhook subscription updated")
        except Exception:
            logger.exception("Failed to update MAX webhook subscription")

    yield


app = FastAPI(title="Доброчат MAX Bot", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
app.include_router(admin_router)
app.include_router(internal_router)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/admin/", status_code=302)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    if settings.max_webhook_secret:
        secret = request.headers.get("X-Max-Bot-Api-Secret")
        if secret != settings.max_webhook_secret:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        update: dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    update_type = update.get("update_type")
    # Диалог с пользователем — обрабатываем до ответа webhook, иначе callback/меню «теряются» в фоне.
    if update_type in {"message_created", "message_callback", "bot_started", "bot_stopped"}:
        try:
            broadcast_id = await process_update(session, update)
            await session.commit()
            if broadcast_id is not None:
                from app.services.broadcast import send_broadcast

                await send_broadcast(session, broadcast_id)
        except Exception:
            await session.rollback()
            logger.exception("Failed to process update: %s", update_type)
            raise HTTPException(status_code=500, detail="update processing failed") from None
        return JSONResponse({"ok": True})

    background_tasks.add_task(_process_update_bg, update)
    logger.debug("Webhook accepted: %s", update_type)
    return JSONResponse({"ok": True})


async def _process_update_bg(update: dict[str, Any]) -> None:
    from app.database import async_session
    from app.services.broadcast import send_broadcast

    async with async_session() as session:
        try:
            broadcast_id = await process_update(session, update)
            await session.commit()
            if broadcast_id is not None:
                await send_broadcast(session, broadcast_id)
        except Exception:
            await session.rollback()
            logger.exception("Failed to process update: %s", update.get("update_type"))
            raise


@app.post("/setup-webhook")
async def setup_webhook(request: Request) -> dict[str, Any]:
    """Одноразовая настройка webhook. Защищено паролем админа."""
    data = await request.json()
    if data.get("password") != settings.admin_password:
        raise HTTPException(status_code=403)

    if not settings.webhook_url:
        raise HTTPException(status_code=400, detail="WEBHOOK_URL not configured")

    result = await max_client.subscribe_webhook(
        settings.webhook_url,
        secret=settings.max_webhook_secret,
    )
    try:
        await setup_bot_commands()
    except Exception:
        logger.exception("Failed to register MAX bot commands during webhook setup")
    return result
