"""
One-click installer setup keys.

A Department/Main Admin downloads a DSA installer from the Deployment Center with a setup key
appended to it. On the lab PC the installer presents the key when it registers its provisioning
session; the session is then treated as registered by the administrator who downloaded it, so
the department policy can auto-approve it without an interactive portal sign-in.

Safety: the key is random (256 bit), single use, expires within 24 hours (default 4), only its
hash is stored, it can only register a session of its own device type for its own department,
and it can be revoked. It never grants an access token by itself: the normal claim flow still
issues the agent credentials.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.utils import timezone

from iic_booking.device_provisioning import policy as policy_mod
from iic_booking.device_provisioning.models import AuditAction
from iic_booking.device_provisioning.models import DeviceType
from iic_booking.device_provisioning.models import InstallerSetupKey

KEY_PREFIX = "dsk_"
logger = logging.getLogger(__name__)

PREFIX_LEN = 12
DEFAULT_TTL = timedelta(hours=4)
MAX_TTL = timedelta(hours=24)
SUPPORTED_DEVICE_TYPES = {DeviceType.DSA}


def issue(*, actor, department, device_type: str = DeviceType.DSA, ttl_hours=None, client_ip=None):
    """Returns (InstallerSetupKey, plaintext). Plaintext is never stored."""
    from iic_booking.device_provisioning.services import write_audit

    if device_type not in SUPPORTED_DEVICE_TYPES:
        raise ValueError("unsupported_device_type")
    if department is None:
        raise ValueError("department_required")
    if not (
        policy_mod.user_belongs_to_department(actor, department)
        and policy_mod.user_has_provisioning_rights(actor, department)
    ):
        raise PermissionError("insufficient_provisioning_rights")

    ttl = DEFAULT_TTL
    if ttl_hours not in (None, ""):
        try:
            ttl = timedelta(hours=float(ttl_hours))
        except (TypeError, ValueError):
            raise ValueError("invalid_ttl") from None
        if ttl <= timedelta(0):
            raise ValueError("invalid_ttl")
        ttl = min(ttl, MAX_TTL)

    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    key = InstallerSetupKey.objects.create(
        device_type=device_type,
        key_hash=make_password(plaintext),
        key_prefix=plaintext[:PREFIX_LEN],
        department=department,
        created_by=actor,
        expires_at=timezone.now() + ttl,
    )
    write_audit(
        action=AuditAction.SETUP_KEY_ISSUED,
        message="One-click installer setup key issued",
        actor=actor,
        detail={
            "setup_key_id": str(key.id),
            "device_type": device_type,
            "department_id": department.pk,
            "expires_at": key.expires_at.isoformat(),
        },
        client_ip=client_ip,
    )
    return key, plaintext


def consume(raw: str, *, device_type: str, client_ip=None, mark: bool = True) -> InstallerSetupKey:
    """
    Atomically marks a valid key used and returns it. Raises ValueError("invalid_setup_key")
    for unknown, expired, revoked, already-used or wrong-device-type keys (one error for all,
    so the installer learns nothing about which).
    """

    raw = (raw or "").strip()
    now = timezone.now()
    if not raw.startswith(KEY_PREFIX) or len(raw) < PREFIX_LEN + 8:
        raise ValueError("invalid_setup_key")

    reason = "unknown"
    rejected_id = None
    with transaction.atomic():
        candidates = list(
            InstallerSetupKey.objects.select_for_update()
            .filter(key_prefix=raw[:PREFIX_LEN])
            .select_related("department", "created_by"),
        )
        for key in candidates:
            if not check_password(raw, key.key_hash):
                continue
            rejected_id = str(key.id)
            if key.used_at is not None:
                reason = "already_used"
            elif key.revoked_at is not None:
                reason = "revoked"
            elif key.expires_at <= now:
                reason = "expired"
            elif key.device_type != device_type:
                reason = "wrong_device_type"
            elif not getattr(key.created_by, "is_active", True):
                reason = "issuer_inactive"
            else:
                if mark:
                    key.used_at = now
                    key.save(update_fields=["used_at"])
                return key
            break

    # create_session runs in one transaction that rolls back on this error, so an audit row
    # would vanish; log instead (no key material).
    logger.warning("One-click setup key rejected | id=%s | reason=%s | ip=%s", rejected_id, reason, client_ip)
    raise ValueError("invalid_setup_key")


def claim(key: InstallerSetupKey) -> None:
    """Marks a key validated with mark=False as used; fails if another request won the race."""
    updated = InstallerSetupKey.objects.filter(
        pk=key.pk, used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now(),
    ).update(used_at=timezone.now())
    if updated != 1:
        raise ValueError("invalid_setup_key")


def mark_session(key: InstallerSetupKey, session, client_ip=None) -> None:
    from iic_booking.device_provisioning.services import write_audit

    key.used_session = session
    key.save(update_fields=["used_session"])
    write_audit(
        action=AuditAction.SETUP_KEY_USED,
        message="One-click installer setup key used",
        session=session,
        actor=key.created_by,
        detail={"setup_key_id": str(key.id), "hostname": session.hostname},
        client_ip=client_ip,
    )


def revoke(key: InstallerSetupKey) -> None:
    if key.revoked_at is None and key.used_at is None:
        key.revoked_at = timezone.now()
        key.save(update_fields=["revoked_at"])
