from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

DADATA_CLEAN_URL = "https://cleaner.dadata.ru/api/v1/clean/address"
DADATA_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"
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
    apartment: str | None
    latitude: float | None
    longitude: float | None
    quality_code: int | None


def _pick_district(data: dict[str, Any]) -> str:
    for key in ("city_district", "city_area", "area", "settlement", "city"):
        value = (data.get(key) or "").strip()
        if value:
            return value
    return UNKNOWN_DISTRICT


def _from_dadata_data(raw: str, data: dict[str, Any], *, normalized: str | None = None) -> NormalizedAddress:
    result = (normalized or data.get("result") or raw).strip()
    qc = data.get("qc")
    if isinstance(qc, str) and qc.isdigit():
        qc = int(qc)
    elif not isinstance(qc, int):
        qc = None

    return NormalizedAddress(
        raw=raw,
        normalized=result,
        district=_pick_district(data),
        region=(data.get("region_with_type") or data.get("region") or "").strip() or None,
        city=(data.get("city") or "").strip() or None,
        settlement=(data.get("settlement") or "").strip() or None,
        street=(data.get("street_with_type") or data.get("street") or "").strip() or None,
        house=(data.get("house") or "").strip() or None,
        apartment=(data.get("flat") or data.get("room") or "").strip() or None,
        latitude=_geo_float(data.get("geo_lat")),
        longitude=_geo_float(data.get("geo_lon")),
        quality_code=qc,
    )


def _geo_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _normalize_via_clean(raw: str, headers: dict[str, str]) -> NormalizedAddress | None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(DADATA_CLEAN_URL, headers=headers, json=[raw])
        if response.status_code == 403:
            return None
        response.raise_for_status()
        payload = response.json()

    if not payload or not isinstance(payload, list):
        return None

    data = payload[0] or {}
    return _from_dadata_data(raw, data)


async def _normalize_via_suggest(raw: str, api_key: str) -> NormalizedAddress | None:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {api_key}",
    }
    body = {
        "query": raw,
        "count": 1,
        "locations": [{"region": "Московская"}],
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(DADATA_SUGGEST_URL, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()

    suggestions = payload.get("suggestions") or []
    if not suggestions:
        return None

    item = suggestions[0]
    data = item.get("data") or {}
    return _from_dadata_data(raw, data, normalized=item.get("value"))


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
        apartment=None,
        latitude=None,
        longitude=None,
        quality_code=None,
    )

    if not settings.dadata_api_key:
        logger.warning("DaData API key not configured, using raw address")
        return fallback

    try:
        if settings.dadata_secret_key:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {settings.dadata_api_key}",
                "X-Secret": settings.dadata_secret_key,
            }
            cleaned = await _normalize_via_clean(raw, headers)
            if cleaned:
                return cleaned

        suggested = await _normalize_via_suggest(raw, settings.dadata_api_key)
        if suggested:
            return suggested
    except Exception:
        logger.exception("DaData request failed for address: %s", raw[:80])

    return fallback
