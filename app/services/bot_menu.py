from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.max_client import main_menu_keyboard, max_client
from app.models import User
from app.services.admspgo_directory import send_directory_to_user
from app.texts import HELP_REGISTERED, MENU_GUEST, MENU_PROMPT

MENU_PAYLOADS = frozenset(
    {
        "menu_directory",
        "menu_appeal",
        "menu_utilities",
        "menu_help",
        "menu_profile",
        "menu_status",
        "menu_open",
        "menu_polls",
        "menu_transport",
    },
)


async def send_main_menu(
    max_user_id: int,
    *,
    registered: bool,
    user: User | None = None,
    text: str | None = None,
) -> None:
    message = text or MENU_PROMPT
    await max_client.send_message(
        max_user_id,
        message,
        attachments=main_menu_keyboard(registered=registered, user=user),
    )


async def try_handle_menu_callback(
    session: AsyncSession,
    user: User,
    payload: str,
) -> bool:
    if payload not in MENU_PAYLOADS:
        return False

    from app.services.appeal_flow import APPEAL_CATEGORY_UTILITIES, APPEAL_FLOW_NONE, try_start_appeal_flow
    from app.services.profile_flow import send_profile_or_block, try_handle_profile_callback as _profile_cb

    in_appeal = (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_NONE
    if in_appeal and payload not in {"menu_directory", "menu_open", "menu_events", "menu_polls", "menu_profile", "menu_status"}:
        await max_client.send_message(
            user.max_user_id,
            "Сначала завершите или отмените обращение (<b>отмена</b>).",
        )
        return True

    if payload == "menu_directory":
        await send_directory_to_user(user.max_user_id)
        await send_main_menu(user.max_user_id, registered=user.is_registered, user=user)
        return True

    if not user.is_registered:
        await max_client.send_message(user.max_user_id, MENU_GUEST)
        await send_main_menu(user.max_user_id, registered=False)
        return True

    if payload == "menu_transport":
        from app.services.transport_order_flow import try_start_transport_order

        await try_start_transport_order(session, user)
        return True

    if payload == "menu_appeal":
        await try_start_appeal_flow(session, user)
        return True

    if payload == "menu_utilities":
        await try_start_appeal_flow(session, user, category=APPEAL_CATEGORY_UTILITIES)
        return True

    if payload == "menu_help":
        await max_client.send_message(
            user.max_user_id,
            HELP_REGISTERED,
            attachments=main_menu_keyboard(registered=True),
        )
        return True

    if payload in {"menu_profile", "menu_status"}:
        await _profile_cb(session, user, payload)
        return True

    if payload == "menu_open":
        await send_main_menu(user.max_user_id, registered=user.is_registered, user=user)
        return True

    return False
