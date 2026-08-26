"""
Phase B booking mutation wrappers (DISABLED by default).

Wrappers must call existing audited domain services only:
- book_equipment / perform_booking_cancellation / user_reschedule_booking

Enablement gates (all required):
1. Phase A acceptance green (`PHASE A READY — MUTATIONS REMAIN DISABLED` first)
2. Explicit env flags COPILOT_BOOKING_* = True
3. Confirmation token + idempotency key on every execute
4. Separate research_copilot_mutation throttle
"""

from __future__ import annotations

from typing import Any

from django.conf import settings


def _flag(name: str) -> bool:
    return bool(getattr(settings, name, False))


def prepare_booking_create(*, user, payload: dict[str, Any]) -> dict[str, Any]:
    """Prepare-only card for Phase A; execute blocked until COPILOT_BOOKING_CREATE."""
    return {
        "ok": True,
        "phase": "prepare",
        "requires_confirmation": True,
        "executable": _flag("COPILOT_BOOKING_CREATE"),
        "message": "Phase A: open portal booking to create. Mutation execute is disabled.",
        "href": f"/book-equipment?equipment={payload.get('equipment_id') or ''}",
    }


def execute_booking_create(*, user, confirmation_token: str, idempotency_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not _flag("COPILOT_BOOKING_CREATE"):
        return {"ok": False, "error": "COPILOT_BOOKING_CREATE_DISABLED", "message": "Booking create via Copilot is disabled."}
    # Phase B: call book_equipment domain path with confirmation + idempotency.
    raise NotImplementedError("Phase B not enabled")


def execute_booking_cancel(*, user, confirmation_token: str, idempotency_key: str, booking_id: int) -> dict[str, Any]:
    if not _flag("COPILOT_BOOKING_CANCEL"):
        return {"ok": False, "error": "COPILOT_BOOKING_CANCEL_DISABLED", "message": "Booking cancel via Copilot is disabled."}
    raise NotImplementedError("Phase B not enabled")


def execute_booking_reschedule(*, user, confirmation_token: str, idempotency_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not _flag("COPILOT_BOOKING_RESCHEDULE"):
        return {"ok": False, "error": "COPILOT_BOOKING_RESCHEDULE_DISABLED", "message": "Reschedule via Copilot is disabled."}
    raise NotImplementedError("Phase B not enabled")
