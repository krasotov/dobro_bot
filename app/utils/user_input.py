from __future__ import annotations

import re


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def is_valid_fio(value: str) -> bool:
    parts = [p for p in value.strip().split() if p]
    return len(parts) >= 2 and len(value.strip()) >= 5


def is_valid_address(value: str) -> bool:
    return len(value.strip()) >= 10


def is_valid_phone_text(value: str) -> bool:
    digits = normalize_phone(value)
    return len(digits) in {10, 11}
