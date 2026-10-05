"""Main Administrator setting for the faculty login wallet sync deadline (audited, no restart).

The login-time sync (faculty_login_wallet_sync) runs while now < deadline. The deadline is
PortalMigrationState.faculty_wallet_sync_cutoff when stored, otherwise the built-in
booking_lock.FACULTY_WALLET_SYNC_CUTOFF. The admin manual Legacy user sync is not gated by it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.users.legacy_ledger.booking_lock import (
    FACULTY_WALLET_SYNC_CUTOFF,
    stored_faculty_wallet_sync_cutoff,
)
from iic_booking.users.models.portal_migration import (
    FacultyWalletSyncCutoffChange,
    PortalMigrationState,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
MAX_AHEAD = timedelta(days=365)
REASON_MAX_LENGTH = 1000
HISTORY_LIMIT = 10


class FacultyWalletSyncDeadlineError(ValueError):
    pass


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(IST).isoformat() if value else None


def _ist_label(value: datetime | None) -> str | None:
    return value.astimezone(IST).strftime("%d %b %Y, %H:%M:%S IST") if value else None


def parse_cutoff(raw) -> datetime | None:
    """Timezone-aware datetime, or None for an empty value (meaning: close the window now)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, datetime):
        value = raw
    else:
        try:
            value = parse_datetime(str(raw).strip())
        except ValueError:
            value = None
        if value is None:
            raise FacultyWalletSyncDeadlineError(
                "Enter the deadline as an ISO 8601 date and time, e.g. 2026-10-31T23:59:59+05:30."
            )
    if timezone.is_naive(value):
        raise FacultyWalletSyncDeadlineError(
            "The deadline must include a timezone offset, e.g. 2026-10-31T23:59:59+05:30."
        )
    return value


def _serialize_change(row: FacultyWalletSyncCutoffChange) -> dict:
    actor = row.actor
    return {
        "id": row.pk,
        "changed_at": _iso(row.created_at),
        "changed_by_name": (getattr(actor, "name", "") or "") if actor else "",
        "changed_by_email": row.actor_email,
        "old_cutoff": _iso(row.old_cutoff),
        "new_cutoff": _iso(row.new_cutoff),
        "reason": row.reason,
    }


def _recent_changes() -> list[dict]:
    try:
        with transaction.atomic():
            rows = list(FacultyWalletSyncCutoffChange.objects.select_related("actor")[:HISTORY_LIMIT])
    except DatabaseError:
        return []
    return [_serialize_change(r) for r in rows]


def faculty_wallet_sync_deadline_status(now: datetime | None = None) -> dict:
    now = now or timezone.now()
    stored = stored_faculty_wallet_sync_cutoff()
    effective = stored or FACULTY_WALLET_SYNC_CUTOFF
    history = _recent_changes()
    return {
        "cutoff": _iso(effective),
        "cutoff_ist": _ist_label(effective),
        "source": "setting" if stored else "default",
        "stored_cutoff": _iso(stored),
        "default_cutoff": _iso(FACULTY_WALLET_SYNC_CUTOFF),
        "window_open": now < effective,
        "server_time": _iso(now),
        "max_cutoff": _iso(now + MAX_AHEAD),
        "last_change": history[0] if history else None,
        "recent_changes": history,
    }


def set_faculty_wallet_sync_cutoff(*, actor, raw_cutoff, reason, now: datetime | None = None) -> FacultyWalletSyncCutoffChange:
    """Store a new deadline and write one audit row. Empty raw_cutoff closes the window now."""
    reason = str(reason or "").strip()
    if not reason:
        raise FacultyWalletSyncDeadlineError("A reason is required.")
    if len(reason) > REASON_MAX_LENGTH:
        raise FacultyWalletSyncDeadlineError(f"Keep the reason under {REASON_MAX_LENGTH} characters.")
    now = now or timezone.now()
    new_cutoff = parse_cutoff(raw_cutoff) or now
    if new_cutoff > now + MAX_AHEAD:
        raise FacultyWalletSyncDeadlineError("The deadline cannot be more than one year ahead.")

    with transaction.atomic():
        PortalMigrationState.objects.get_or_create(singleton_key="default")
        state = PortalMigrationState.objects.select_for_update().get(singleton_key="default")
        old_cutoff = state.faculty_wallet_sync_cutoff
        state.faculty_wallet_sync_cutoff = new_cutoff
        state.save(update_fields=["faculty_wallet_sync_cutoff", "updated_at"])
        change = FacultyWalletSyncCutoffChange.objects.create(
            old_cutoff=old_cutoff,
            new_cutoff=new_cutoff,
            actor=actor,
            actor_email=(getattr(actor, "email", "") or "")[:255],
            reason=reason,
        )
    logger.info(
        "Faculty wallet sync deadline changed by admin=%s old=%s new=%s audit_id=%s",
        getattr(actor, "pk", None),
        _iso(old_cutoff),
        _iso(new_cutoff),
        change.pk,
    )
    return change
