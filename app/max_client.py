from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import settings


class MaxApiError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class MaxClient:
    def __init__(self) -> None:
        self.base_url = settings.max_api_base.rstrip("/")
        self.token = settings.max_bot_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self.token,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(
            verify=settings.max_api_verify_ssl,
            timeout=30.0,
        ) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                params=params,
                json=json,
            )
        if response.status_code >= 400:
            raise MaxApiError(response.status_code, response.text)
        if not response.content:
            return None
        return response.json()

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def get_chat(self, chat_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/chats/{chat_id}")

    async def set_commands(self, commands: list[dict[str, str]]) -> dict[str, Any]:
        return await self._request("PATCH", "/me/commands", json={"commands": commands})

    async def get_upload_url(self, upload_type: str = "image") -> dict[str, Any]:
        return await self._request("POST", "/uploads", params={"type": upload_type})

    async def upload_image(self, file_bytes: bytes, filename: str) -> str:
        upload_info = await self.get_upload_url("image")
        upload_url = upload_info["url"]
        token = upload_info.get("token")

        async with httpx.AsyncClient(
            verify=settings.max_api_verify_ssl,
            timeout=120.0,
        ) as client:
            response = await client.post(
                upload_url,
                files={"data": (filename, file_bytes)},
            )

        if response.status_code >= 400:
            raise MaxApiError(response.status_code, response.text)

        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            token = payload.get("token") or token

        if not token:
            parsed = urlparse(upload_url)
            token = parse_qs(parsed.query).get("token", [None])[0]

        if not token:
            raise MaxApiError(500, "MAX API did not return image token")

        await asyncio.sleep(0.5)
        return token

    async def upload_file(self, file_bytes: bytes, filename: str) -> str:
        upload_info = await self.get_upload_url("file")
        upload_url = upload_info["url"]
        token = upload_info.get("token")

        async with httpx.AsyncClient(
            verify=settings.max_api_verify_ssl,
            timeout=120.0,
        ) as client:
            response = await client.post(
                upload_url,
                files={"data": (filename, file_bytes)},
            )

        if response.status_code >= 400:
            raise MaxApiError(response.status_code, response.text)

        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            token = payload.get("token") or token

        if not token:
            parsed = urlparse(upload_url)
            token = parse_qs(parsed.query).get("token", [None])[0]

        if not token:
            raise MaxApiError(500, "MAX API did not return file token")

        await asyncio.sleep(0.5)
        return token

    async def deliver_message(
        self,
        user_id: int,
        text: str,
        *,
        callback_id: str | None = None,
        chat_id: int | None = None,
        attachments: list[dict[str, Any]] | None = None,
        link: dict[str, Any] | None = None,
        format: str = "html",
        menu_bar: bool = True,
        notification: str | None = None,
    ) -> dict[str, Any]:
        """Ack callback и новое сообщение в диалог (без edit через answer — так стабильнее в клиенте MAX)."""
        att = attachments
        if menu_bar:
            att = ensure_menu_bar(att)

        if callback_id:
            toast = notification if notification is not None and str(notification).strip() else "✓"
            try:
                await self.answer_callback(callback_id, notification=toast)
            except MaxApiError:
                try:
                    await self.answer_callback(callback_id)
                except MaxApiError:
                    pass

        try:
            return await self.send_message(
                user_id,
                text,
                chat_id=chat_id,
                attachments=att,
                link=link,
                format=format,
                menu_bar=False,
            )
        except MaxApiError:
            if chat_id is None:
                raise
            return await self.send_message(
                user_id,
                text,
                attachments=att,
                link=link,
                format=format,
                menu_bar=False,
            )

    async def send_message(
        self,
        user_id: int,
        text: str,
        *,
        chat_id: int | None = None,
        attachments: list[dict[str, Any]] | None = None,
        link: dict[str, Any] | None = None,
        format: str = "html",
        menu_bar: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "format": format}
        if menu_bar:
            attachments = ensure_menu_bar(attachments)
        attachments = normalize_inline_keyboard_attachments(attachments)
        if attachments:
            body["attachments"] = attachments
        if link:
            body["link"] = link
        params: dict[str, Any] = {}
        if chat_id is not None:
            params["chat_id"] = chat_id
        else:
            params["user_id"] = user_id
        result = await self._request(
            "POST",
            "/messages",
            params=params,
            json=body,
        )
        if isinstance(result, dict):
            msg = result.get("message")
            if isinstance(msg, dict):
                recipient = msg.get("recipient") if isinstance(msg.get("recipient"), dict) else {}
                cid = recipient.get("chat_id")
                uid = recipient.get("user_id") or user_id
                if cid is not None and uid is not None:
                    try:
                        from app.services.dialog_inbox_poll import remember_dialog_chat

                        remember_dialog_chat(int(uid), int(cid))
                    except Exception:
                        pass
        return result

    async def forward_message(self, user_id: int, forward_mid: str) -> dict[str, Any]:
        last_error: MaxApiError | None = None
        for link in (
            {"type": "forward", "mid": forward_mid},
            {"type": "forward", "message_id": forward_mid},
        ):
            try:
                return await self._request(
                    "POST",
                    "/messages",
                    params={"user_id": user_id},
                    json={"text": "", "link": link},
                )
            except MaxApiError as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise MaxApiError(500, "Failed to forward message")

    async def send_message_with_image(
        self,
        user_id: int,
        text: str,
        *,
        image_token: str | None = None,
        image_url: str | None = None,
        format: str = "html",
        extra_attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        attachments: list[dict[str, Any]] | None = None
        if image_token or image_url:
            payload: dict[str, Any] = {}
            if image_token:
                payload["token"] = image_token
            elif image_url:
                payload["url"] = image_url
            attachments = [{"type": "image", "payload": payload}]
        if extra_attachments:
            attachments = (attachments or []) + extra_attachments

        last_error: MaxApiError | None = None
        for attempt in range(4):
            try:
                return await self.send_message(
                    user_id,
                    text,
                    attachments=attachments,
                    format=format,
                )
            except MaxApiError as exc:
                last_error = exc
                if "attachment.not.ready" in exc.message.lower() and attempt < 3:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise

        if last_error:
            raise last_error
        raise MaxApiError(500, "Failed to send message")

    async def answer_callback(
        self,
        callback_id: str,
        *,
        message: dict[str, Any] | None = None,
        notification: str | None = None,
        menu_bar: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if message:
            msg = dict(message)
            if menu_bar and msg.get("attachments"):
                msg["attachments"] = ensure_menu_bar(msg["attachments"])
            body["message"] = msg
        if notification is not None and str(notification).strip():
            body["notification"] = notification
        if not body:
            body["notification"] = "\u200b"
        return await self._request(
            "POST",
            "/answers",
            params={"callback_id": callback_id},
            json=body,
        )

    async def get_updates(
        self,
        *,
        marker: int | None = None,
        limit: int = 100,
        timeout: int = 25,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "timeout": timeout}
        if marker is not None:
            params["marker"] = marker
        result = await self._request("GET", "/updates", params=params)
        return result if isinstance(result, dict) else {"updates": result or []}

    async def subscribe_webhook(
        self,
        url: str,
        *,
        secret: str,
        update_types: list[str] | None = None,
    ) -> dict[str, Any]:
        types = update_types or [
            "bot_added",
            "bot_removed",
            "bot_started",
            "bot_stopped",
            "message_created",
            "message_callback",
        ]
        return await self._request(
            "POST",
            "/subscriptions",
            json={"url": url, "secret": secret, "update_types": types},
        )

    async def get_subscriptions(self) -> dict[str, Any]:
        return await self._request("GET", "/subscriptions")

    async def delete_subscription(self, url: str) -> None:
        await self._request("DELETE", "/subscriptions", params={"url": url})


max_client = MaxClient()

MENU_OPEN_PAYLOAD = "menu_open"
MENU_BUTTON_TEXT = "📋 Меню"
MENU_MESSAGE_TEXT = "меню"


def message_button(text: str) -> dict[str, str]:
    return {"type": "message", "text": text}

BOT_COMMANDS: list[dict[str, str]] = [
    {"name": "menu", "description": "Главное меню"},
    {"name": "appeal", "description": "Новое обращение"},
    {"name": "help", "description": "Помощь и подсказки"},
    {"name": "profile", "description": "Профиль и адрес"},
    {"name": "events", "description": "Мероприятия"},
    {"name": "polls", "description": "Опросы"},
    {"name": "utilities", "description": "Обращение по ЖКХ"},
]


def _is_menu_trigger_button(button: dict[str, Any]) -> bool:
    if button.get("type") == "callback" and str(button.get("payload") or "") == MENU_OPEN_PAYLOAD:
        return True
    if button.get("type") == "message":
        label = str(button.get("text") or button.get("payload") or "").strip().lower()
        return label in {"меню", "menu", "/menu"}
    return False


def _keyboard_has_menu_button(buttons: list[list[dict[str, Any]]]) -> bool:
    for row in buttons:
        for button in row:
            if _is_menu_trigger_button(button):
                return True
    return False


def _normalize_keyboard_buttons(buttons: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    normalized: list[list[dict[str, Any]]] = []
    for row in buttons:
        new_row: list[dict[str, Any]] = []
        for button in row:
            btn = dict(button)
            if btn.get("type") == "callback" and "intent" not in btn:
                btn["intent"] = "default"
            new_row.append(btn)
        normalized.append(new_row)
    return normalized


def normalize_inline_keyboard_attachments(
    attachments: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    if not attachments:
        return attachments
    result: list[dict[str, Any]] = []
    for item in attachments:
        if item.get("type") != "inline_keyboard":
            result.append(item)
            continue
        payload = dict(item.get("payload") or {})
        buttons = payload.get("buttons")
        if isinstance(buttons, list):
            payload["buttons"] = _normalize_keyboard_buttons([list(row) for row in buttons])
        result.append({**item, "payload": payload})
    return result


def menu_bar_keyboard() -> list[dict[str, Any]]:
    return normalize_inline_keyboard_attachments(
        [
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [
                        [{"type": "callback", "text": MENU_BUTTON_TEXT, "payload": MENU_OPEN_PAYLOAD}],
                    ],
                },
            }
        ]
    ) or []


def ensure_menu_bar(attachments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not attachments:
        return menu_bar_keyboard()

    merged = [dict(item) for item in attachments]
    for item in merged:
        if item.get("type") != "inline_keyboard":
            continue
        payload = dict(item.get("payload") or {})
        buttons = [list(row) for row in payload.get("buttons") or []]
        if _keyboard_has_menu_button(buttons):
            return merged
        buttons.append([{"type": "callback", "text": MENU_BUTTON_TEXT, "payload": MENU_OPEN_PAYLOAD}])
        item["payload"] = {**payload, "buttons": buttons}
        return merged

    return [*merged, *menu_bar_keyboard()]


async def setup_bot_commands() -> None:
    await max_client.set_commands(BOT_COMMANDS)


def consent_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {"type": "callback", "text": "Согласен", "payload": "consent_yes"},
                        {"type": "callback", "text": "Отказаться", "payload": "consent_no"},
                    ]
                ]
            },
        }
    ]


def contact_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [{"type": "request_contact", "text": "Поделиться контактом"}],
                    [{"type": "callback", "text": "Ввести номер вручную", "payload": "phone_manual"}],
                ]
            },
        }
    ]


def flow_cancel_keyboard(cancel_payload: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [{"type": "callback", "text": "Отмена", "payload": cancel_payload}],
                ],
            },
        }
    ]


def append_cancel_row(
    attachments: list[dict[str, Any]] | None,
    cancel_payload: str,
) -> list[dict[str, Any]]:
    cancel_row = [{"type": "callback", "text": "Отмена", "payload": cancel_payload}]
    if not attachments:
        return flow_cancel_keyboard(cancel_payload)

    result: list[dict[str, Any]] = []
    merged = False
    for item in attachments:
        item_copy = dict(item)
        if item_copy.get("type") != "inline_keyboard":
            result.append(item_copy)
            continue
        payload = dict(item_copy.get("payload") or {})
        buttons = [list(row) for row in payload.get("buttons") or []]
        if any(btn.get("payload") == cancel_payload for row in buttons for btn in row):
            return attachments
        buttons.append(cancel_row)
        item_copy["payload"] = {**payload, "buttons": buttons}
        result.append(item_copy)
        merged = True
    if not merged:
        result.extend(flow_cancel_keyboard(cancel_payload))
    return result


def appeal_cancel_keyboard() -> list[dict[str, Any]]:
    return flow_cancel_keyboard("appeal_cancel")


def appeal_attach_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {"type": "callback", "text": "Отправить", "payload": "appeal_submit"},
                        {"type": "callback", "text": "Отмена", "payload": "appeal_cancel"},
                    ],
                ],
            },
        }
    ]


def transport_cancel_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {"type": "callback", "text": "Отмена", "payload": "transport_cancel"},
                        message_button("отмена"),
                    ],
                ],
            },
        }
    ]


def profile_cancel_keyboard() -> list[dict[str, Any]]:
    return flow_cancel_keyboard("profile_cancel")


def appeal_operator_cancel_keyboard() -> list[dict[str, Any]]:
    return flow_cancel_keyboard("appeal_operator_cancel")


def service_note_cancel_keyboard() -> list[dict[str, Any]]:
    return flow_cancel_keyboard("service_note_cancel")


def service_note_it_notify_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {
                            "type": "callback",
                            "text": "🛠 Заявки ИТ",
                            "payload": "menu_service_notes_ops",
                        }
                    ],
                ],
            },
        }
    ]


def service_note_attach_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {"type": "callback", "text": "Отправить", "payload": "service_note_submit"},
                        {"type": "callback", "text": "Отмена", "payload": "service_note_cancel"},
                    ],
                ],
            },
        }
    ]


def service_note_operator_cancel_keyboard() -> list[dict[str, Any]]:
    return flow_cancel_keyboard("service_note_operator_cancel")


def service_note_user_review_keyboard(note_id: int) -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {"type": "callback", "text": "Принять работу", "payload": f"sn_user_accept_{note_id}"},
                        {"type": "callback", "text": "Проблема осталась", "payload": f"sn_user_reject_{note_id}"},
                    ],
                ],
            },
        }
    ]


def utilities_disclaimer_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [{"type": "callback", "text": "Продолжить", "payload": "utilities_continue"}],
                    [{"type": "callback", "text": "Отмена", "payload": "appeal_cancel"}],
                ]
            },
        }
    ]


def profile_edit_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {"type": "callback", "text": "ФИО", "payload": "profile_edit_fio"},
                        {"type": "callback", "text": "Дата рожд.", "payload": "profile_edit_birth"},
                    ],
                    [
                        {"type": "callback", "text": "Адрес", "payload": "profile_edit_address"},
                        {"type": "callback", "text": "Телефон", "payload": "profile_edit_phone"},
                    ],
                    [{"type": "callback", "text": "В меню", "payload": "menu_open"}],
                ]
            },
        }
    ]


def transport_confirm_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {"type": "callback", "text": "Отправить заявку", "payload": "transport_confirm"},
                        {"type": "callback", "text": "Отмена", "payload": "transport_cancel"},
                        message_button("отмена"),
                    ]
                ]
            },
        }
    ]


def main_menu_keyboard(*, registered: bool, user: Any | None = None) -> list[dict[str, Any]]:
    """Главное меню бота (inline-кнопки под сообщением)."""
    from app.services.audience_roles import (
        user_can_order_transport,
        user_is_appeals_operator,
        user_is_garage_operator,
        user_is_it_operator,
    )

    buttons: list[list[dict[str, Any]]] = [
        [{"type": "callback", "text": "Справочник", "payload": "menu_directory"}],
    ]
    if registered:
        if user and user_can_order_transport(user):
            buttons.append(
                [{"type": "callback", "text": "🚗 Заказать авто", "payload": "menu_transport"}]
            )
        if user and user_is_garage_operator(user):
            buttons.append(
                [{"type": "callback", "text": "🚗 Заявки на авто", "payload": "menu_transport_ops"}]
            )
        if user and user_is_appeals_operator(user):
            buttons.append(
                [{"type": "callback", "text": "📋 Обращения", "payload": "menu_appeals_ops"}]
            )
        if user and user_is_it_operator(user):
            buttons.append(
                [{"type": "callback", "text": "🛠 Заявки ИТ", "payload": "menu_service_notes_ops"}]
            )
        buttons.append([{"type": "callback", "text": "Обращение", "payload": "menu_appeal"}])
        buttons.append(
            [
                {"type": "callback", "text": "Мероприятия", "payload": "menu_events"},
                {"type": "callback", "text": "Опросы", "payload": "menu_polls"},
            ]
        )
        buttons.append(
            [{"type": "callback", "text": "Подписки", "payload": "menu_subscriptions"}]
        )
        buttons.append(
            [
                {"type": "callback", "text": "Помощь", "payload": "menu_help"},
                {"type": "callback", "text": "Профиль", "payload": "menu_profile"},
            ]
        )
    buttons.append([{"type": "callback", "text": MENU_BUTTON_TEXT, "payload": MENU_OPEN_PAYLOAD}])
    return normalize_inline_keyboard_attachments(
        [
            {
                "type": "inline_keyboard",
                "payload": {"buttons": buttons},
            }
        ]
    ) or []


def topic_subscriptions_keyboard(
    topics: list[dict[str, Any]],
    subscribed: set[str],
) -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = []
    row: list[dict[str, Any]] = []
    for topic in topics:
        slug = str(topic.get("slug") or "")
        title = str(topic.get("title") or slug)
        if not slug:
            continue
        mark = "✓ " if slug in subscribed else ""
        row.append(
            {
                "type": "callback",
                "text": f"{mark}{title}"[:64],
                "payload": f"topic_toggle:{slug}",
            }
        )
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"type": "callback", "text": "← Меню", "payload": "menu_open"}])
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def events_list_keyboard(events: list[dict]) -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = []
    for event in events:
        title = str(event.get("title") or "Мероприятие")[:64]
        post_id = str(event.get("id") or "")
        if not post_id:
            continue
        rows.append([{"type": "callback", "text": title, "payload": f"event_pick:{post_id}"}])
    rows.append([{"type": "callback", "text": "← Меню", "payload": "menu_open"}])
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def event_detail_keyboard(post_id: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [{"type": "callback", "text": "Записаться", "payload": f"event_register:{post_id}"}],
                    [
                        {"type": "callback", "text": "← К списку", "payload": "events_back"},
                        {"type": "callback", "text": "Меню", "payload": "menu_open"},
                    ],
                ]
            },
        }
    ]


def _poll_vote_buttons(
    poll_id: str,
    options: list[dict[str, Any]],
    *,
    broadcast_id: int | None,
) -> list[list[dict[str, Any]]]:
    bid = str(broadcast_id or 0)
    rows: list[list[dict[str, Any]]] = []
    for opt in options:
        key = str(opt.get("key") or "").strip()
        label = str(opt.get("label") or key).strip()
        if not key or not label:
            continue
        rows.append(
            [
                {
                    "type": "callback",
                    "text": label[:48],
                    "payload": f"poll_vote:{poll_id}:{key}:{bid}",
                }
            ]
        )
    return rows


def poll_vote_keyboard(
    poll_id: str,
    options: list[dict[str, Any]],
    *,
    broadcast_id: int | None,
) -> list[dict[str, Any]]:
    rows = _poll_vote_buttons(poll_id, options, broadcast_id=broadcast_id)
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def polls_list_keyboard(polls: list[dict]) -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = []
    for poll in polls:
        title = str(poll.get("title") or "Опрос")[:48]
        poll_id = str(poll.get("id") or "")
        if not poll_id:
            continue
        rows.append([{"type": "callback", "text": title, "payload": f"poll_pick:{poll_id}"}])
    rows.append([{"type": "callback", "text": "← Меню", "payload": "menu_open"}])
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def poll_detail_keyboard(
    poll_id: str,
    options: list[dict[str, Any]],
    *,
    broadcast_id: int | None,
) -> list[dict[str, Any]]:
    rows = _poll_vote_buttons(poll_id, options, broadcast_id=broadcast_id)
    rows.append(
        [
            {"type": "callback", "text": "← К списку", "payload": "polls_back"},
            {"type": "callback", "text": "Меню", "payload": "menu_open"},
        ]
    )
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]
