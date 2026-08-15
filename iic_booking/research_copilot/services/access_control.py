"""Access modes and tool ACL for Research Copilot (AI.24.1).

THE LLM IS NEVER THE AUTHORIZATION AUTHORITY.
Backend decides access_mode and which tools may run.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any


class AccessMode(str, Enum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"


class ToolAccessLevel(str, Enum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    AUTHORIZED_RESOURCE = "authorized_resource"
    MUTATION = "mutation"


LOGIN_REQUIRED_MESSAGE = (
    "Please sign in so I can access your account information "
    "(bookings, samples, results, wallet, PI rates, or Remote Analysis). "
    "Public facility information remains available without signing in."
)

PUBLIC_PRICING_LOGIN_HINT = (
    "Please sign in so I can calculate the rate applicable to your account "
    "(including PI or wallet-owner rates when configured)."
)

_PRIVATE_INTENT_RE = re.compile(
    r"\b("
    r"my (?:next )?booking|my bookings|my sample|my result|my results|"
    r"my wallet|my balance|my pi rate|pi rate for me|"
    r"cancel my|change my booking|analyze my|my (?:pxrd|xrd|fesem) data|"
    r"start remote analysis|launch remote|my analyzed|"
    r"wallet balance|another user|other user|someone else'?s"
    r")\b",
    re.I,
)


def is_authenticated_user(user) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False))


def resolve_access_mode(*, user) -> AccessMode:
    if is_authenticated_user(user):
        return AccessMode.AUTHENTICATED
    return AccessMode.PUBLIC


def private_intent_requires_login(*, text: str, access_mode: AccessMode) -> bool:
    """True when an anonymous (or public-only) ask needs a private portal tool."""
    if access_mode == AccessMode.AUTHENTICATED:
        return False
    lower = (text or "").lower()
    if _PRIVATE_INTENT_RE.search(lower):
        return True
    # Possessive booking/result phrasing
    if re.search(r"\b(my|our)\b.+\b(booking|sample|result|wallet|session)\b", lower):
        return True
    return False


def tool_allowed_for_mode(*, access_level: str | ToolAccessLevel, access_mode: AccessMode) -> bool:
    if isinstance(access_level, ToolAccessLevel):
        level = access_level
    else:
        level = ToolAccessLevel(str(access_level))
    mode = access_mode if isinstance(access_mode, AccessMode) else AccessMode(str(access_mode))
    if mode == AccessMode.PUBLIC:
        return level == ToolAccessLevel.PUBLIC
    # Authenticated may use all levels; resource ownership still enforced in handlers.
    return True


def sanitize_anonymous_key(raw: str | None) -> str | None:
    key = re.sub(r"[^a-zA-Z0-9_-]", "", (raw or "").strip())[:64]
    if len(key) < 16:
        return None
    return key


def public_bootstrap_prompts() -> list[str]:
    return [
        "What is XRD?",
        "What XRD facilities are available?",
        "How much does 5 PXRD samples cost?",
        "How do I submit an XRD sample?",
        "What software is used for PXRD analysis?",
        "Can external researchers use IIC facilities?",
    ]


def strip_internal_infra(text: str) -> str:
    """Best-effort redaction of infra hints in model output (defense in depth)."""
    if not text:
        return text
    patterns = [
        re.compile(r"https?://[^\s]*11434[^\s]*", re.I),
        re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"),
        re.compile(r"(?i)guacamole[^\n]{0,80}"),
        re.compile(r"(?i)ollama\s*url[^\n]{0,80}"),
        re.compile(r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*\S+"),
    ]
    out = text
    for pat in patterns:
        out = pat.sub("[redacted]", out)
    return out
