"""The I-STEM FBR number line shown in booking emails when FBR is mandatory for the booking."""

import logging

logger = logging.getLogger(__name__)

FBR_LABEL = "FBR number"


def booking_requires_fbr(booking) -> bool:
    from .models import charge_profile_requires_istem_fbr

    if booking is None:
        return False
    if getattr(booking, "istem_fbr_status", None) is not None:
        return True
    try:
        return charge_profile_requires_istem_fbr(getattr(booking, "charge_profile", None))
    except Exception:
        return False


def booking_fbr_number_for_email(booking) -> str:
    """The FBR number when FBR is mandatory and has been entered, else ""."""
    if booking is None:
        return ""
    number = str(getattr(booking, "istem_fbr_number", "") or "").strip()
    if not number or not booking_requires_fbr(booking):
        return ""
    return number


def fbr_text_line(booking) -> str:
    number = booking_fbr_number_for_email(booking)
    return f"{FBR_LABEL}: {number}" if number else ""


def apply_fbr_number_to_context(context: dict, booking) -> dict:
    if isinstance(context, dict):
        context["fbr_number"] = booking_fbr_number_for_email(booking)
    return context


def fbr_number_from_context(context) -> str:
    """FBR number for a rendered email: from ``fbr_number`` when set, else looked up from the booking ID."""
    if not isinstance(context, dict):
        return ""
    if "fbr_number" in context:
        return str(context.get("fbr_number") or "").strip()
    ref = str(context.get("virtual_booking_id") or context.get("booking_id") or "").strip()
    if not ref:
        return ""
    try:
        from .models import Booking

        qs = Booking.objects.select_related("charge_profile")
        booking = qs.filter(virtual_booking_id=ref).first()
        if booking is None and ref.isdigit():
            booking = qs.filter(booking_id=int(ref)).first()
        return booking_fbr_number_for_email(booking)
    except Exception:
        logger.debug("FBR lookup for email failed (ref=%s)", ref, exc_info=True)
        return ""
