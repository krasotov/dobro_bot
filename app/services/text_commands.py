from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.utils.bot_command import normalize_bot_command

# Команды с кнопок type=message (когда callback/webhook у пользователя недоступны).
_MENU_TEXTS = frozenset({"меню", "menu", "start"})
_IT_LIST_TEXTS = frozenset({"заявки ит", "it", "it заявки", "заявки it", "обновить ит"})
async def try_handle_text_command(
    session: AsyncSession,
    user: User,
    text: str,
    *,
    chat_id: int | None,
) -> bool:
    from app.services.update_dedupe import should_skip_user_command

    raw = text.strip()
    if not raw:
        return False

    cmd = normalize_bot_command(raw)

    if cmd in _MENU_TEXTS:
        if should_skip_user_command(user.max_user_id, cmd):
            return True
        from app.services.bot_menu import send_main_menu

        await send_main_menu(
            user.max_user_id,
            registered=user.is_registered,
            user=user,
            chat_id=chat_id,
        )
        return True

    if cmd in _IT_LIST_TEXTS:
        if should_skip_user_command(user.max_user_id, "заявки ит"):
            return True
        from app.services.audience_roles import user_is_it_operator
        from app.services.service_note_operator_flow import send_operator_notes_list

        if user.is_registered and user_is_it_operator(user):
            await send_operator_notes_list(session, user, chat_id=chat_id)
            return True
        return False

    if user.is_registered:
        from app.services.audience_roles import (
            user_is_appeals_operator,
            user_is_garage_operator,
            user_is_it_operator,
        )
        from app.services.service_note_operator_flow import try_handle_service_note_operator_text

        if user_is_it_operator(user) and await try_handle_service_note_operator_text(
            session, user, raw, chat_id=chat_id
        ):
            return True

        if cmd == "справочник":
            from app.services.admspgo_directory import send_directory_to_user
            from app.services.bot_menu import send_main_menu

            await send_directory_to_user(user.max_user_id)
            await send_main_menu(
                user.max_user_id, registered=True, user=user, chat_id=chat_id
            )
            return True

        if cmd == "обращение":
            from app.services.appeal_flow import try_start_appeal_flow

            await try_start_appeal_flow(session, user)
            return True

        if cmd in {"мероприятия", "мероприятие", "events"}:
            from app.services.event_flow import send_events_list

            await send_events_list(user.max_user_id)
            return True

        if cmd in {"опросы", "опрос", "polls"}:
            from app.services.poll_flow import send_polls_list

            await send_polls_list(user.max_user_id)
            return True

        if cmd in {"подписки", "subscriptions", "topics"}:
            from app.services.topic_subscriptions import send_subscriptions_panel

            await send_subscriptions_panel(session, user)
            return True

        if cmd in {"помощь", "help"}:
            from app.max_client import main_menu_keyboard, max_client
            from app.texts import HELP_REGISTERED

            await max_client.send_message(
                user.max_user_id,
                HELP_REGISTERED,
                attachments=main_menu_keyboard(registered=True, user=user),
                chat_id=chat_id,
                menu_bar=False,
            )
            return True

        if cmd in {"профиль", "profile", "статус", "status"}:
            from app.services.appeal_flow import APPEAL_FLOW_NONE
            from app.services.profile_flow import send_profile_or_block

            in_appeal = (user.appeal_flow_state or APPEAL_FLOW_NONE) != APPEAL_FLOW_NONE
            await send_profile_or_block(session, user, in_appeal=in_appeal)
            return True

        if cmd in {"заказать авто", "авто"}:
            from app.services.audience_roles import user_can_order_transport
            from app.services.transport_order_flow import try_start_transport_order

            if user_can_order_transport(user):
                await try_start_transport_order(session, user)
                return True

        if cmd in {"заявки авто", "заявки на авто"} and user_is_garage_operator(user):
            from app.services.transport_operator_flow import send_operator_orders_list

            await send_operator_orders_list(session, user)
            return True

        if cmd in {"обращения", "обращения оп"} and user_is_appeals_operator(user):
            from app.services.appeal_operator_flow import send_operator_appeals_list

            await send_operator_appeals_list(user)
            return True

    return False
