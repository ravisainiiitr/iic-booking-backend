"""Masking for anything the integration logs or stores: credentials removed, personal data reduced."""

from __future__ import annotations

from typing import Any

# Never kept in any form.
SECRET_KEYS = frozenset({"password", "username", "access_token", "token", "authorization"})
# Personal or billing data: only a short masked hint is kept.
PERSONAL_KEYS = frozenset({
    "user_first_name", "user_last_name", "user_email", "user_contactno", "user_address", "user_city", "user_district",
    "user_pin", "billing_user_name", "billing_address", "booking_gstn", "gstin", "user_salutation", "user_gender",
    "payer_name", "payer_email", "payer_phone",
})


def mask(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"{local[:1]}***@{domain}"
    if len(text) <= 4:
        return "***"
    return f"{text[:2]}***{text[-2:]}"


def redact(data: Any) -> Any:
    """Copy of ``data`` with secrets removed and personal fields masked (dicts and lists, recursively)."""
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            k = str(key).lower()
            if k in SECRET_KEYS:
                out[key] = "[removed]"
            elif k in PERSONAL_KEYS:
                out[key] = mask(value)
            else:
                out[key] = redact(value)
        return out
    if isinstance(data, list):
        return [redact(item) for item in data]
    return data
