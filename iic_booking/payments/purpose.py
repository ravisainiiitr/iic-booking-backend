"""What a payment is for, in plain words, shown on the payment page, in Razorpay Checkout and on the receipt."""

from __future__ import annotations

# Fabrication profiles (3D printing, 2D laser cutting) are "Fabrication charges"; everything else is analysis.
PURPOSE_BY_PROFILE = {
    "PRINT_3D": "Fabrication charges",
    "LASER_CUT_2D": "Fabrication charges",
}
DEFAULT_PURPOSE = "Analysis charges"


def profile_type_of(booking=None, equipment=None) -> str:
    """The booking's charge profile type, else the equipment's own."""
    profile = getattr(booking, "charge_profile", None) if booking is not None else None
    kind = getattr(profile, "effective_profile_type", None) if profile is not None else None
    if not kind and equipment is not None:
        kind = getattr(equipment, "profile_type", None)
    return str(kind or "")


def booking_purpose(equipment_name: str, profile_type: str = "") -> str:
    """e.g. "Analysis charges for 'Powder X-Ray Diffractometer'" or "Fabrication charges for '3D Printer'"."""
    label = PURPOSE_BY_PROFILE.get(str(profile_type or "").upper(), DEFAULT_PURPOSE)
    name = (equipment_name or "").strip()
    return f"{label} for '{name}'" if name else label


def recharge_purpose(department_name: str = "") -> str:
    department = (department_name or "").strip()
    return f"Wallet recharge for the {department} sub-wallet" if department else "Wallet recharge"


def order_purpose(order) -> str:
    """Purpose of a payment order, from its booking, external checkout or wallet recharge."""
    from .models import PaymentPurpose

    if order.purpose == PaymentPurpose.WALLET_RECHARGE:
        return recharge_purpose(getattr(order.department, "name", ""))
    booking = order.booking
    checkout = getattr(order, "external_checkout", None) if booking is None else None
    if booking is None and checkout is not None:
        booking = checkout.booking
    equipment = getattr(booking, "equipment", None) or getattr(checkout, "equipment", None)
    return booking_purpose(getattr(equipment, "name", ""), profile_type_of(booking, equipment))
