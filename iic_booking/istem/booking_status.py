"""What the booking pages show about a booking's I-STEM record when the portal handles it automatically (2 Nov 2026).

``automatic_status(booking)`` is None unless automatic FBR is on and the booking's charge profile requires an I-STEM
FBR. Otherwise it is a small dict for the booking APIs: the step reached and the I-STEM identifiers. Database reads only.
"""

from __future__ import annotations

from . import conf
from .choices import SyncState

# step: queued -> registered -> confirmed -> executed; or cancelled / attention (held for the IIC team).
_STEP_BY_STATE = {
    SyncState.CREATED: "registered",
    SyncState.CONFIRM_READY: "registered",
    SyncState.CONFIRM_IN_FLIGHT: "registered",
    SyncState.CONFIRMED: "confirmed",
    SyncState.EXECUTE_READY: "confirmed",
    SyncState.EXECUTE_IN_FLIGHT: "confirmed",
    SyncState.EXECUTED_PENDING_VERIFY: "executed",
    SyncState.EXECUTED: "executed",
    SyncState.CANCEL_READY: "cancelled",
    SyncState.CANCEL_IN_FLIGHT: "cancelled",
    SyncState.REMOTE_CANCELLED: "cancelled",
}
_ATTENTION = {
    SyncState.CREATE_FAILED, SyncState.CREATE_UNCERTAIN, SyncState.CONFIRM_FAILED, SyncState.CONFIRM_UNCERTAIN,
    SyncState.CONFIRMED_ID_MISSING, SyncState.EXECUTE_FAILED, SyncState.EXECUTE_UNCERTAIN, SyncState.MANUAL_REVIEW,
    SyncState.BLOCKED_CONFIG, SyncState.REMOTE_REJECTED, SyncState.LOCAL_CLOSED_REMOTE_OPEN, SyncState.CANCEL_FAILED,
    SyncState.CANCEL_UNCERTAIN,
}
LABELS = {
    "queued": "Being registered on I-STEM",
    "registered": "Registered on I-STEM",
    "confirmed": "Slot confirmed on I-STEM",
    "executed": "FBR executed on I-STEM",
    "cancelled": "Cancelled on I-STEM",
    "attention": "With the IIC team for I-STEM",
}


def automatic_status(booking) -> dict | None:
    from iic_booking.equipment.models import charge_profile_requires_istem_fbr

    if not conf.automatic_fbr_enabled() or not charge_profile_requires_istem_fbr(getattr(booking, "charge_profile", None)):
        return None
    try:
        link = booking.istem_link
    except Exception:  # noqa: BLE001 - RelatedObjectDoesNotExist: not sent yet
        link = None
    state = getattr(link, "sync_state", None)
    if state in _ATTENTION or getattr(link, "needs_review", False):
        step = "attention"
    else:
        step = _STEP_BY_STATE.get(state, "queued")
    return {
        "automatic": True,
        "step": step,
        "label": LABELS[step],
        "fbr_no": (getattr(link, "fbr_no", None) or "") if link else "",
        "request_id": (getattr(link, "request_id", None) or "") if link else "",
        "confirmation_id": (getattr(link, "confirmation_id", None) or "") if link else "",
        "payment_id": (getattr(link, "payment_id", None) or "") if link else "",
    }
