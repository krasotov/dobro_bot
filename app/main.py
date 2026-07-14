from contextlib import asynccontextmanager
import logging
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app.admin.routes import router as admin_router
from app.config import settings
from app.database import get_session, init_db
from app.handlers.webhook import process_update
from app.max_client import max_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Доброчат MAX Bot", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
app.include_router(admin_router)


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

    background_tasks.add_task(_process_update_bg, update)
    logger.debug("Webhook accepted: %s", update.get("update_type"))
    return JSONResponse({"ok": True})


async def _process_update_bg(update: dict[str, Any]) -> None:
    from app.database import async_session

    async with async_session() as session:
        try:
            await process_update(session, update)
            await session.commit()
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
    return result
