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


_BILLING = ("billing_user_name", "billing_address", "billing_state")


def waiting_reasons(booking) -> tuple[list[str], list[str]]:
    """Why a booking that should go to I-STEM has not been registered yet: (reasons for the user, detail for staff).
    Uses the same eligibility and Create Booking checks as the sync; database reads only."""
    from . import eligibility, mapping
    from .models import IstemUserLink

    ok, reason = eligibility.evaluate(booking)
    if not ok:
        if reason == "payment not settled":
            return ["Waiting for the payment to be confirmed."], [reason]
        if reason.startswith("booking status is"):
            return ["Registered on I-STEM once the booking is confirmed and paid."], [reason]
        return ["Not sent to I-STEM for this booking."], [reason]
    user_link = IstemUserLink.objects.filter(user=booking.user).first()
    built = mapping.build_create_booking_payload(booking, user_link)
    if built.ok:
        return ["Being registered on I-STEM; this usually takes a few minutes."], []
    errors = built.errors
    reasons = []
    detail_extra = []
    if "public_user_id" in errors:
        reasons.append(_link_reason(booking.user, user_link, detail_extra,
                                    include_address=not any(k in errors for k in _BILLING)))
    if any(k in errors for k in _BILLING):
        reasons.append("Your billing details are incomplete (billing name, address and state). Add them in your profile.")
    if "equipment_id" in errors:
        reasons.append("This instrument is not on I-STEM, so the IIC team handles its I-STEM record.")
    elif set(errors) - {"public_user_id", "booking_gstn", *_BILLING}:
        reasons.append("Waiting for the IIC team to complete this instrument's I-STEM setup.")
    detail = [f"{key}: {message}" for key, message in sorted(errors.items())] + detail_extra
    return reasons or ["Waiting for the IIC team."], detail


def _link_reason(user, user_link, detail: list[str], *, include_address: bool) -> str:
    """Why the user has no I-STEM user id yet. [USER 2026-11-08] the portal links the account itself (Create User, or
    the existing I-STEM user for the email/mobile), so the user is only asked for profile data that step lacks."""
    state = getattr(user_link, "state", "") or ""
    if user_link is not None and user_link.review_reason:
        detail.append(f"I-STEM user link ({state}): {user_link.review_reason}")
    if not conf.user_creation_enabled():
        return "Your I-STEM account is not linked yet. The IIC team links it; the booking is then registered automatically."
    from .booking_gate import _create_user_problems

    problems = _create_user_problems(user, include_address=include_address)
    if problems:
        return ("Your I-STEM account is created automatically once your profile has: "
                + " ".join(p["message"] for p in problems))
    if state == "NEEDS_REVIEW":
        return "The IIC team is checking your I-STEM account; the booking is then registered automatically."
    return "Your I-STEM account is being set up automatically; the booking is then registered on I-STEM."


def automatic_status(booking, *, include_reason: bool = False) -> dict | None:
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
    result = {
        "automatic": True,
        "step": step,
        "label": LABELS[step],
        "fbr_no": (getattr(link, "fbr_no", None) or "") if link else "",
        "request_id": (getattr(link, "request_id", None) or "") if link else "",
        "confirmation_id": (getattr(link, "confirmation_id", None) or "") if link else "",
        "payment_id": (getattr(link, "payment_id", None) or "") if link else "",
    }
    if include_reason and step == "queued":
        try:
            result["waiting_reasons"], result["waiting_detail"] = waiting_reasons(booking)
        except Exception:  # the booking page must never fail because of this explanation
            result["waiting_reasons"], result["waiting_detail"] = [], []
    return result
