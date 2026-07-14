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
        format: str = "html",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "format": format}
        if attachments:
            body["attachments"] = attachments
        return await self._request(
            "POST",
            "/messages",
            params={"user_id": user_id},
            json=body,
        )

    async def send_message_with_image(
        self,
        user_id: int,
        text: str,
        *,
        image_token: str | None = None,
        format: str = "html",
    ) -> dict[str, Any]:
        attachments = None
        if image_token:
            attachments = [{"type": "image", "payload": {"token": image_token}}]

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
