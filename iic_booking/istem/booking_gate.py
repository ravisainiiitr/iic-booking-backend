"""Stop a booking before payment when the user's profile lacks what I-STEM needs (8 Nov 2026).

The portal registers bookings on I-STEM itself (automatic FBR). A booking whose user is not linked to I-STEM, or whose
billing details are incomplete, would be paid for and then wait indefinitely. So, for instruments that are on I-STEM and
whose charge profile requires an FBR, the booking is refused up front with what to complete in the profile.

Scope (``ISTEM_BOOKING_GATE``): ``off``; ``test_accounts`` (default, [USER 2026-11-08] test accounts first); ``all``.

Not checked here, because the user cannot fix them: instrument mapping and codes (the IIC team's setup), the institute
billing settings for internal bookings, and a missing GSTIN (I-STEM receives the agreed placeholder instead; no dummy
GSTIN is ever stored in a profile).
"""

from __future__ import annotations

from django.conf import settings

from . import conf
from .mapping import ALPHA_SPACES, GSTIN

GATE_OFF, GATE_TEST_ACCOUNTS, GATE_ALL = "off", "test_accounts", "all"
PROFILE_PATH = "/profile"


def gate_scope() -> str:
    value = str(getattr(settings, "ISTEM_BOOKING_GATE", GATE_TEST_ACCOUNTS) or "").strip().lower()
    return value if value in (GATE_OFF, GATE_TEST_ACCOUNTS, GATE_ALL) else GATE_TEST_ACCOUNTS


def _applies_to(user) -> bool:
    scope = gate_scope()
    if scope == GATE_OFF:
        return False
    if scope == GATE_ALL:
        return True
    from iic_booking.users.test_accounts import is_test_user

    return is_test_user(user)


def _instrument_on_istem(equipment) -> bool:
    from .models import IstemEquipmentMapping

    return IstemEquipmentMapping.objects.filter(equipment=equipment, is_active=True, verified_at__isnull=False).exists()


def billing_problems(user) -> list[dict]:
    """What is missing or malformed in an external user's billing profile, as I-STEM's Create Booking needs it."""
    billing = getattr(user, "external_billing_profile", None)
    if billing is None:
        return [{"field": "billing", "message": "Add your billing details (billing name, address, city, pincode and state)."}]
    out: list[dict] = []
    name = (billing.billing_name or "").strip()
    if not name:
        out.append({"field": "billing_name", "message": "Billing name is empty."})
    elif not ALPHA_SPACES.match(name):
        out.append({"field": "billing_name", "message": "Billing name may contain letters and spaces only (an I-STEM rule)."})
    for field, label in (("billing_address_line1", "Billing address line 1"), ("billing_city", "Billing city"),
                         ("billing_pincode", "Billing pincode"), ("billing_state", "Billing state")):
        if not (getattr(billing, field, "") or "").strip():
            out.append({"field": field, "message": f"{label} is empty."})
    gstin = (billing.gstin or "").strip().upper()
    if gstin and not GSTIN.match(gstin):
        out.append({"field": "gstin", "message": "GSTIN must be 15 letters and digits, or left empty if you have none."})
    return out


def profile_problems(user, *, internal: bool) -> list[dict]:
    """Everything the user must complete before an I-STEM booking. Internal users are billed to the institute, so only
    the I-STEM link applies to them (unless they keep a billing profile of their own, e.g. an incubated startup)."""
    from .models import IstemUserLink

    out: list[dict] = []
    link = IstemUserLink.objects.filter(user=user).first()
    if link is None or link.state != "LINKED" or not str(link.public_user_id or "").isdigit():
        out.append({"field": "istem_link", "message": "Link your portal account to your I-STEM account."})
    if not internal or getattr(user, "external_billing_profile", None) is not None:
        out.extend(billing_problems(user))
    return out


def refusal(user, equipment, charge_profile) -> dict | None:
    """The refusal body when this booking must not proceed, else None."""
    if not getattr(charge_profile, "require_istem_fbr", False) or not conf.automatic_fbr_enabled():
        return None
    if not _applies_to(user) or not _instrument_on_istem(equipment):
        return None
    from iic_booking.external_services import checkout_context

    ctx = checkout_context.current()
    if ctx is not None and ctx.mode == checkout_context.CONFIRM:
        # Already checked at the quote, before payment; a paid booking is never refused for this.
        return None
    from iic_booking.users.models.user_type import UserType

    problems = profile_problems(user, internal=UserType.is_internal_user(getattr(user, "user_type", "") or ""))
    if not problems:
        return None
    message = ("This instrument's bookings are registered on the national I-STEM portal. Complete your profile first: "
               + " ".join(p["message"] for p in problems))
    return {"error": message, "message": message, "code": "ISTEM_PROFILE_INCOMPLETE", "missing": problems,
            "profile_url": PROFILE_PATH}


def readiness(user, equipment=None, charge_profile=None) -> dict:
    """For the profile and booking pages: what is missing, and whether a booking would be refused now."""
    from iic_booking.users.models.user_type import UserType

    internal = UserType.is_internal_user(getattr(user, "user_type", "") or "")
    problems = profile_problems(user, internal=internal)
    blocks = bool(equipment is not None and charge_profile is not None and refusal(user, equipment, charge_profile))
    return {"complete": not problems, "missing": problems, "internal": internal, "blocks_booking": blocks,
            "gate": gate_scope(), "applies_to_you": _applies_to(user), "profile_url": PROFILE_PATH}
