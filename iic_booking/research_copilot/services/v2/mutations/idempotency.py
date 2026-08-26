"""Idempotency for Copilot Phase B mutations (cache-backed)."""

from __future__ import annotations

import hashlib
from typing import Any

from django.core.cache import cache

IDEMPOTENCY_TTL_SECONDS = 24 * 3600


def _key(*, user_id: int, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{user_id}:{idempotency_key}".encode()).hexdigest()[:48]
    return f"copilot_idem:{digest}"


def get_cached_result(*, user, idempotency_key: str) -> dict[str, Any] | None:
    if not idempotency_key:
        return None
    data = cache.get(_key(user_id=int(user.pk), idempotency_key=str(idempotency_key)))
    return data if isinstance(data, dict) else None


def store_result(*, user, idempotency_key: str, result: dict[str, Any]) -> None:
    if not idempotency_key:
        return
    cache.set(
        _key(user_id=int(user.pk), idempotency_key=str(idempotency_key)),
        result,
        IDEMPOTENCY_TTL_SECONDS,
    )


def make_idempotency_key(*, user, action: str, proposal_id: str) -> str:
    return f"copilot:{user.pk}:{action}:{proposal_id}"
