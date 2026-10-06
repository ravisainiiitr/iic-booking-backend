"""Parsing and validation of the support desk recipient lists (no model imports, so models can use it)."""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

MAX_ALERT_RECIPIENTS = 20


def split_emails(raw) -> list[str]:
    """Comma, semicolon, whitespace or one-per-line list (or an actual list) -> trimmed non-empty parts."""
    if isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = re.split(r"[\s,;]+", str(raw or ""))
    return [p.strip() for p in parts if p and p.strip()]


def clean_email_list(raw) -> tuple[list[str], list[str]]:
    """(valid addresses de-duplicated case-insensitively in input order, invalid entries)."""
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for addr in split_emails(raw):
        try:
            validate_email(addr)
        except ValidationError:
            invalid.append(addr)
            continue
        if addr.lower() not in seen:
            seen.add(addr.lower())
            valid.append(addr)
    return valid, invalid


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if local and domain else "***"
