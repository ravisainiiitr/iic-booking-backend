"""Limit password guessing on the email/password login (security review, Nov 2026).

Failed attempts are counted per account and per client address for a 15-minute window; once either limit is reached,
the login answers 429 until the window passes. A successful login clears the account's count. The client address is
the last X-Forwarded-For hop (the one Apache adds), so a client cannot dodge the limit by sending its own header.
"""

from __future__ import annotations

import hashlib

from django.core.cache import cache

WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_ACCOUNT = 10
MAX_FAILURES_PER_ADDRESS = 50


def _client_address(request) -> str:
    meta = getattr(request, "META", {}) or {}
    forwarded = [p.strip() for p in (meta.get("HTTP_X_FORWARDED_FOR") or "").split(",") if p.strip()]
    return forwarded[-1] if forwarded else str(meta.get("REMOTE_ADDR") or "unknown")


def _keys(request, email: str) -> tuple[str, str]:
    account = hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()[:32]
    return f"login-fail:acct:{account}", f"login-fail:addr:{_client_address(request)}"


def _count(key: str) -> int:
    return int(cache.get(key) or 0)


def is_blocked(request, email: str) -> bool:
    account_key, address_key = _keys(request, email)
    return _count(account_key) >= MAX_FAILURES_PER_ACCOUNT or _count(address_key) >= MAX_FAILURES_PER_ADDRESS


def record_failure(request, email: str) -> None:
    for key in _keys(request, email):
        if not cache.add(key, 1, timeout=WINDOW_SECONDS):
            try:
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, timeout=WINDOW_SECONDS)


def clear(request, email: str) -> None:
    cache.delete(_keys(request, email)[0])
