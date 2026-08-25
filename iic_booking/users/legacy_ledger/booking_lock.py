"""Backend booking lock for end-user booking during portal cutover."""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from iic_booking.users.legacy_ledger.schema_gate import safe_portal_migration_state
from iic_booking.users.models import UserType
from iic_booking.users.models.portal_migration import PortalMigrationState


def format_booking_lock_message(state: PortalMigrationState | None = None) -> str:
    if state is None:
        state, _ = safe_portal_migration_state()
    opens = state.booking_opens_at
    if opens:
        local = timezone.localtime(opens) if timezone.is_aware(opens) else opens
        date_s = local.strftime("%d %B %Y")
        time_s = local.strftime("%H:%M")
    else:
        date_s = "[CONFIGURED DATE]"
        time_s = "[CONFIGURED TIME]"
    template = state.booking_lock_message or (
        "New IIC Equipment Booking Portal\n\n"
        "The new portal is currently being prepared for launch.\n\n"
        "Online equipment booking will be available from:\n\n"
        "    {date}\n"
        "    {time}\n\n"
        "Until then, please continue using the existing IIC Booking Portal.\n\n"
        "Your wallet migration is being synchronized and your wallet "
        "balance and transaction history will remain available."
    )
    return template.replace("{date}", date_s).replace("{time}", time_s)


def end_user_booking_is_locked(user) -> tuple[bool, str]:
    """Staff types are not locked. Students/faculty/external/other end users are locked when flag is off."""
    ut = getattr(user, "user_type", None)
    if not (UserType.is_end_user_booking_type(ut) or ut == UserType.OTHER):
        return False, ""
    state, _ = safe_portal_migration_state()
    enabled = state.end_user_booking_enabled
    if enabled:
        return False, ""
    return True, format_booking_lock_message(state)


OLD_PORTAL_MIGRATION_BANNER = (
    "IIC Booking has migrated to the new portal. "
    "New bookings are now available only through the new IIC Booking Portal. "
    "You can continue to view your previous bookings and account information here."
)

# Modes where the OLD (external) portal must reject create/reschedule/waitlist/sample.
LEGACY_PORTAL_BOOKING_DISABLED_MODES = frozenset(
    {"FREEZE", "ACTIVE", "SETTLEMENT", "COMPLETED"}
)


def legacy_portal_mutating_booking_blocked() -> tuple[bool, str, str]:
    """
    OLD portal (external) create/reschedule/waitlist/sample gate.
    Returns (blocked, code, message).
    """
    state, _ = safe_portal_migration_state()
    mode = (getattr(state, "booking_migration_mode", None) or "NORMAL").upper()
    if mode in LEGACY_PORTAL_BOOKING_DISABLED_MODES:
        return (
            True,
            "MIGRATION_BOOKING_DISABLED",
            "New bookings are disabled on the previous booking portal during migration. "
            "Please use the new IIC Booking Portal.",
        )
    return False, "", ""


def booking_status_payload(user=None) -> dict:
    state, schema = safe_portal_migration_state()
    locked = False
    message = ""
    if user is not None:
        locked, message = end_user_booking_is_locked(user)
    else:
        locked = not state.end_user_booking_enabled
        message = format_booking_lock_message(state) if locked else ""
    mode = (getattr(state, "booking_migration_mode", None) or "NORMAL").upper()
    old_portal_disabled = mode in LEGACY_PORTAL_BOOKING_DISABLED_MODES
    start = getattr(state, "migration_start_at", None)
    end = getattr(state, "migration_window_end_at", None)
    return {
        "end_user_booking_enabled": state.end_user_booking_enabled,
        "locked_for_this_user": locked,
        "message": message,
        "code": "MIGRATION_BOOKING_NOT_ACTIVE" if locked else "",
        "booking_opens_at": state.booking_opens_at.isoformat() if state.booking_opens_at else None,
        "phase": state.phase,
        "legacy_ledger_frozen": state.legacy_ledger_frozen,
        "last_wallet_txn_watermark": state.last_wallet_txn_watermark,
        "environment": getattr(settings, "DEPLOYMENT_ENVIRONMENT", "UNKNOWN"),
        # Phase 8B — central migration mode (do not scatter independent booleans)
        "booking_migration_mode": mode,
        "migration_start_at": start.isoformat() if start else None,
        "migration_window_end_at": end.isoformat() if end else None,
        "new_portal_url": getattr(state, "new_portal_url", "") or "",
        "schema_gate": schema.get("gate"),
        # Signal for OLD portal / bridge consumers (this Django app is the NEW portal).
        "legacy_portal_new_booking_disabled": old_portal_disabled,
        "legacy_portal_booking_disabled_code": (
            "MIGRATION_BOOKING_DISABLED" if old_portal_disabled else ""
        ),
        "legacy_portal_migration_banner": OLD_PORTAL_MIGRATION_BANNER if old_portal_disabled else "",
        "new_portal_slot_protection_active": mode in {"PREPARATION", "FREEZE", "ACTIVE", "SETTLEMENT"},
    }
