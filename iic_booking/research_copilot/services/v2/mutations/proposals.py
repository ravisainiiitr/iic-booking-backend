"""Booking / cancel / reschedule proposal store (cache-backed, no migration)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from django.core.cache import cache
from django.utils import timezone

PROPOSAL_TTL_SECONDS = 15 * 60
POLICY_VERSION = "copilot-v2-phase-b-1"


def _key(proposal_id: str) -> str:
    return f"copilot_proposal:{proposal_id}"


def create_proposal(
    *,
    user,
    action: str,
    payload: dict[str, Any],
    ttl_seconds: int = PROPOSAL_TTL_SECONDS,
) -> dict[str, Any]:
    """Create a bound proposal. Payload must not include secrets."""
    proposal_id = str(uuid.uuid4())
    confirmation_token = secrets.token_urlsafe(24)
    now = timezone.now()
    expires = now + timedelta(seconds=ttl_seconds)
    record = {
        "proposal_id": proposal_id,
        "confirmation_token": confirmation_token,
        "action": action,
        "user_id": int(user.pk),
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "policy_version": POLICY_VERSION,
        "payload": payload,
        "status": "READY_FOR_CONFIRMATION",
        "payload_fingerprint": _fingerprint(payload),
    }
    cache.set(_key(proposal_id), record, ttl_seconds)
    # Also index by user for "confirm" without re-stating id (latest only)
    cache.set(f"copilot_proposal_latest:{user.pk}:{action}", proposal_id, ttl_seconds)
    return record


def get_proposal(proposal_id: str) -> dict[str, Any] | None:
    if not proposal_id:
        return None
    data = cache.get(_key(str(proposal_id)))
    return data if isinstance(data, dict) else None


def get_latest_proposal(*, user, action: str) -> dict[str, Any] | None:
    pid = cache.get(f"copilot_proposal_latest:{user.pk}:{action}")
    return get_proposal(str(pid)) if pid else None


def invalidate_proposal(proposal_id: str) -> None:
    cache.delete(_key(str(proposal_id)))


def validate_proposal_for_user(
    *,
    user,
    proposal_id: str,
    confirmation_token: str,
    expected_action: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Return (proposal, error_code).
    Rejects wrong user, wrong token, expired, action mismatch, or missing.
    """
    prop = get_proposal(proposal_id)
    if not prop:
        return None, "PROPOSAL_NOT_FOUND"
    if int(prop.get("user_id") or 0) != int(user.pk):
        return None, "PROPOSAL_FORBIDDEN"
    if confirmation_token and prop.get("confirmation_token") != confirmation_token:
        return None, "CONFIRMATION_INVALID"
    if not confirmation_token:
        return None, "CONFIRMATION_REQUIRED"
    if expected_action and prop.get("action") != expected_action:
        return None, "PROPOSAL_ACTION_MISMATCH"
    try:
        exp = datetime.fromisoformat(prop["expires_at"])
        if timezone.is_naive(exp):
            exp = timezone.make_aware(exp, timezone.get_current_timezone())
        if timezone.now() > exp:
            return None, "PROPOSAL_EXPIRED"
    except Exception:  # noqa: BLE001
        return None, "PROPOSAL_EXPIRED"
    return prop, None


def _fingerprint(payload: dict[str, Any]) -> str:
    raw = repr(sorted((payload or {}).items())).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:32]
