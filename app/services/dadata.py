from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

DADATA_CLEAN_URL = "https://cleaner.dadata.ru/api/v1/clean/address"
UNKNOWN_DISTRICT = "Не определён"


@dataclass
class NormalizedAddress:
    raw: str
    normalized: str
    district: str
    region: str | None
    city: str | None
    settlement: str | None
    street: str | None
    house: str | None
    quality_code: int | None


def _pick_district(data: dict[str, Any]) -> str:
    for key in ("city_district", "city_area", "area", "settlement", "city"):
        value = (data.get(key) or "").strip()
        if value:
            return value
    return UNKNOWN_DISTRICT


async def normalize_address(raw_address: str) -> NormalizedAddress:
    raw = raw_address.strip()
    fallback = NormalizedAddress(
        raw=raw,
        normalized=raw,
        district=UNKNOWN_DISTRICT,
        region=None,
        city=None,
        settlement=None,
        street=None,
        house=None,
        quality_code=None,
    )

    if not settings.dadata_api_key or not settings.dadata_secret_key:
        logger.warning("DaData keys not configured, using raw address")
        return fallback

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {settings.dadata_api_key}",
        "X-Secret": settings.dadata_secret_key,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                DADATA_CLEAN_URL,
                headers=headers,
                json=[raw],
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        logger.exception("DaData request failed for address: %s", raw[:80])
        return fallback

    if not payload or not isinstance(payload, list):
        return fallback

    data = payload[0] or {}
    result = (data.get("result") or raw).strip()
    district = _pick_district(data)

    return NormalizedAddress(
        raw=raw,
        normalized=result,
        district=district,
        region=(data.get("region") or "").strip() or None,
        city=(data.get("city") or "").strip() or None,
        settlement=(data.get("settlement") or "").strip() or None,
        street=(data.get("street") or "").strip() or None,
        house=(data.get("house") or "").strip() or None,
        quality_code=data.get("qc"),
    )
