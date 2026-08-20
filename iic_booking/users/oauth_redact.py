"""Redact OAuth/OIDC secrets before logging. Never log token bodies."""

from __future__ import annotations

from typing import Any

REDACTED = "[REDACTED]"

SECRET_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "authorization_code",
    "code",
    "client_secret",
    "client_secret_basic",
    "password",
    "secret",
    "assertion",
    "session_state",
    "cookie",
}


def _is_secret_key(key: str) -> bool:
    k = str(key).lower().replace("-", "_")
    if k in SECRET_KEYS:
        return True
    if k.endswith("_token") or k.endswith("_secret") or k.endswith("_code"):
        return True
    if "access_token" in k or "refresh_token" in k or "id_token" in k:
        return True
    if k in {"authorization", "auth"}:
        return True
    return False


def redact_oauth_payload(value: Any) -> Any:
    """Return a JSON-serializable copy with secrets replaced."""
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if _is_secret_key(key):
                out[str(key)] = REDACTED if val not in (None, "") else val
            else:
                out[str(key)] = redact_oauth_payload(val)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_oauth_payload(v) for v in value]
    if isinstance(value, str) and _looks_like_bearer(value):
        return REDACTED
    return value


def _looks_like_bearer(text: str) -> bool:
    t = text.strip()
    return t.lower().startswith("bearer ") and len(t) > 20


def redact_oauth_text(text: str | None) -> str:
    if not text:
        return ""
    raw = str(text)
    lowered = raw.lower()
    for marker in (
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "authorization_code",
    ):
        if marker in lowered:
            return REDACTED
    if "bearer " in lowered and len(raw) > 40:
        return REDACTED
    return raw


def userinfo_key_summary(user_info: dict | None) -> dict:
    """Safe shape of Channel-I userinfo: keys only, no PII values, no tokens."""
    info = user_info if isinstance(user_info, dict) else {}
    nested = {}
    for key, val in info.items():
        if _is_secret_key(key):
            continue
        if isinstance(val, dict):
            nested[str(key)] = sorted(str(k) for k in val.keys() if not _is_secret_key(k))
        elif isinstance(val, list):
            nested[str(key)] = f"list[{len(val)}]"
        else:
            nested[str(key)] = type(val).__name__
    return {"top_level_keys": sorted(nested.keys()), "nested_keys": nested}
