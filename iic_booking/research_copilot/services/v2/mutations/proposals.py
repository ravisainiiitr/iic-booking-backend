"""Booking / cancel / reschedule proposal store (cache-backed, no migration).

Confirmation tokens are `<nonce>.<hmac>` where the HMAC (keyed with SECRET_KEY) covers the proposal id,
owning user, action, payload fingerprint and expiry. A token is therefore useless for another user,
another proposal, an edited payload or after expiry, and execution claims a proposal once so a
double click cannot run it twice.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

PROPOSAL_TTL_SECONDS = 15 * 60
BOOKING_CREATE_TTL_SECONDS = 10 * 60
CLAIM_TTL_SECONDS = 120
POLICY_VERSION = "copilot-v2-phase-b-1"
BINDING_VERSION = 2

_ACTION_TTLS = {"CREATE_BOOKING": BOOKING_CREATE_TTL_SECONDS}


def _key(proposal_id: str) -> str:
    return f"copilot_proposal:{proposal_id}"


def _claim_key(proposal_id: str) -> str:
    return f"copilot_proposal_claim:{proposal_id}"


def _sign(*, proposal_id: str, user_id: int, action: str, fingerprint: str, expires_at: str, nonce: str) -> str:
    msg = "|".join((str(proposal_id), str(int(user_id)), str(action), str(fingerprint), str(expires_at), str(nonce)))
    key = f"research-copilot-proposal:{settings.SECRET_KEY}".encode()
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def create_proposal(
    *,
    user,
    action: str,
    payload: dict[str, Any],
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Create a bound proposal. Payload must not include secrets."""
    ttl = int(ttl_seconds or _ACTION_TTLS.get(action) or PROPOSAL_TTL_SECONDS)
    proposal_id = str(uuid.uuid4())
    now = timezone.now()
    expires_at = (now + timedelta(seconds=ttl)).isoformat()
    fingerprint = _fingerprint(payload)
    nonce = secrets.token_urlsafe(18)
    signature = _sign(
        proposal_id=proposal_id,
        user_id=int(user.pk),
        action=action,
        fingerprint=fingerprint,
        expires_at=expires_at,
        nonce=nonce,
    )
    record = {
        "proposal_id": proposal_id,
        "confirmation_token": f"{nonce}.{signature}",
        "action": action,
        "user_id": int(user.pk),
        "created_at": now.isoformat(),
        "expires_at": expires_at,
        "policy_version": POLICY_VERSION,
        "binding_version": BINDING_VERSION,
        "payload": payload,
        "status": "READY_FOR_CONFIRMATION",
        "payload_fingerprint": fingerprint,
    }
    cache.set(_key(proposal_id), record, ttl)
    # Also index by user for "confirm" without re-stating id (latest only)
    cache.set(f"copilot_proposal_latest:{user.pk}:{action}", proposal_id, ttl)
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


def claim_proposal(proposal_id: str) -> bool:
    """Atomically mark a proposal as executing; False when another request already holds it."""
    return bool(cache.add(_claim_key(str(proposal_id)), "1", CLAIM_TTL_SECONDS))


def release_claim(proposal_id: str) -> None:
    cache.delete(_claim_key(str(proposal_id)))


def _binding_ok(prop: dict[str, Any], token: str) -> bool:
    nonce, _, signature = str(token).partition(".")
    if not nonce or not signature:
        return False
    if _fingerprint(prop.get("payload") or {}) != prop.get("payload_fingerprint"):
        return False
    expected = _sign(
        proposal_id=str(prop.get("proposal_id") or ""),
        user_id=int(prop.get("user_id") or 0),
        action=str(prop.get("action") or ""),
        fingerprint=str(prop.get("payload_fingerprint") or ""),
        expires_at=str(prop.get("expires_at") or ""),
        nonce=nonce,
    )
    return hmac.compare_digest(expected, signature)


def validate_proposal_for_user(
    *,
    user,
    proposal_id: str,
    confirmation_token: str,
    expected_action: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Return (proposal, error_code).
    Rejects wrong user, wrong token, tampered payload, expired, action mismatch, or missing.
    """
    prop = get_proposal(proposal_id)
    if not prop:
        return None, "PROPOSAL_NOT_FOUND"
    if int(prop.get("user_id") or 0) != int(user.pk):
        return None, "PROPOSAL_FORBIDDEN"
    if not confirmation_token:
        return None, "CONFIRMATION_REQUIRED"
    stored = str(prop.get("confirmation_token") or "")
    if not stored or not hmac.compare_digest(stored.encode(), str(confirmation_token).encode()):
        return None, "CONFIRMATION_INVALID"
    if str(prop.get("proposal_id") or proposal_id) != str(proposal_id):
        return None, "CONFIRMATION_INVALID"
    if prop.get("binding_version") == BINDING_VERSION and not _binding_ok(prop, confirmation_token):
        return None, "CONFIRMATION_INVALID"
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
