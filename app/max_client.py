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

    async def send_message(
        self,
        user_id: int,
        text: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        link: dict[str, Any] | None = None,
        format: str = "html",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "format": format}
        if attachments:
            body["attachments"] = attachments
        if link:
            body["link"] = link
        return await self._request(
            "POST",
            "/messages",
            params={"user_id": user_id},
            json=body,
        )

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
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if message:
            body["message"] = message
        if notification:
            body["notification"] = notification
        return await self._request(
            "POST",
            "/answers",
            params={"callback_id": callback_id},
            json=body or None,
        )

    async def subscribe_webhook(
        self,
        url: str,
        *,
        secret: str,
        update_types: list[str] | None = None,
    ) -> dict[str, Any]:
        types = update_types or [
            "bot_added",
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


def utilities_disclaimer_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [{"type": "callback", "text": "Продолжить", "payload": "utilities_continue"}],
                    [{"type": "callback", "text": "Отмена", "payload": "utilities_cancel"}],
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
                    ]
                ]
            },
        }
    ]


def main_menu_keyboard(*, registered: bool, user: Any | None = None) -> list[dict[str, Any]]:
    """Главное меню бота (inline-кнопки под сообщением)."""
    from app.services.audience_roles import user_can_order_transport, user_is_transport_operator

    buttons: list[list[dict[str, Any]]] = [
        [{"type": "callback", "text": "Справочник", "payload": "menu_directory"}],
    ]
    if registered:
        if user and user_can_order_transport(user):
            buttons.append(
                [{"type": "callback", "text": "🚗 Заказать авто", "payload": "menu_transport"}]
            )
        if user and user_is_transport_operator(user):
            buttons.append(
                [{"type": "callback", "text": "🚗 Заявки на авто", "payload": "menu_transport_ops"}]
            )
        buttons.append(
            [
                {"type": "callback", "text": "Обращение", "payload": "menu_appeal"},
                {"type": "callback", "text": "ЖКХ", "payload": "menu_utilities"},
            ]
        )
        buttons.append(
            [
                {"type": "callback", "text": "Мероприятия", "payload": "menu_events"},
                {"type": "callback", "text": "Опросы", "payload": "menu_polls"},
            ]
        )
        buttons.append(
            [
                {"type": "callback", "text": "Подписки", "payload": "menu_subscriptions"},
            ]
        )
        buttons.append(
            [
                {"type": "callback", "text": "Помощь", "payload": "menu_help"},
                {"type": "callback", "text": "Профиль", "payload": "menu_profile"},
            ]
        )
    return [
        {
            "type": "inline_keyboard",
            "payload": {"buttons": buttons},
        }
    ]


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
