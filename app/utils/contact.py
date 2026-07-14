from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def normalize_vcf(vcf_info: str) -> str:
    return vcf_info.replace("\\r\\n", "\r\n").replace("\\n", "\n")


def parse_phone_from_vcf(vcf_info: str) -> str | None:
    vcf = normalize_vcf(vcf_info)
    match = re.search(r"TEL[^:\r\n]*:(\+?\d[\d\s\-()]+)", vcf, re.IGNORECASE)
    if not match:
        return None
    return re.sub(r"\D", "", match.group(1))


def verify_contact_hash(vcf_info: str, hash_value: str, token: str) -> bool:
    if not hash_value or not vcf_info or not token:
        return False
    vcf = normalize_vcf(vcf_info)
    expected = hmac.new(token.encode(), vcf.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.lower(), hash_value.lower())


def extract_contact_phone(
    attachments: list[dict[str, Any]],
    *,
    bot_token: str = "",
) -> dict[str, Any] | None:
    for attachment in attachments:
        if attachment.get("type") != "contact":
            continue

        payload = attachment.get("payload") or {}
        vcf_info = payload.get("vcf_info") or ""
        hash_value = payload.get("hash")
        max_info = payload.get("max_info") or {}

        phone = (
            payload.get("vcf_phone")
            or payload.get("phone")
            or max_info.get("phone")
        )
        if not phone and vcf_info:
            phone = parse_phone_from_vcf(vcf_info)

        if not phone:
            logger.warning("Contact attachment without phone: %s", payload)
            continue

        verified = False
        if hash_value and vcf_info and bot_token and verify_contact_hash(vcf_info, hash_value, bot_token):
            verified = True
        elif vcf_info or payload.get("vcf_phone"):
            # Контакт через кнопку request_contact всегда содержит vcf_info.
            verified = True

        return {"phone": str(phone), "verified": verified, "payload": payload}

    return None
