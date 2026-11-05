"""OIC request registration: the equipment's Officer In-charge books a slot in their own name for the lab's own work.

Switched on per equipment by the Main Administrator ("Allow OIC to Register Equipment Requests"). The request is an
ordinary booking (same slot, sample-trace, completion and history lifecycle; status Booked as for any confirmed
booking) recorded against the OIC, with no wallet charge and outside the OIC's weekly / monthly booking limits. The
booking's CREATED event carries ``metadata["oic_registered"] = True``.
"""

from __future__ import annotations

from decimal import Decimal

METADATA_KEY = "oic_registered"
NO_CHARGE_NOTE = "Registered by the Officer In-charge — no charge"


def oic_registration_requested(request) -> bool:
    raw = (getattr(request, "data", None) or {}).get("oic_registration")
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("1", "true", "yes")


def oic_registration_refusal(user, equipment) -> str | None:
    """None when ``user`` may register a request for ``equipment``, else the reason."""
    from .oic_only_equipment import is_oic_only, raise_refusal

    if is_oic_only(equipment):
        return raise_refusal(user, equipment)
    if not getattr(equipment, "oic_request_registration_enabled", False):
        return "Request registration by the Officer In-charge is not switched on for this equipment."
    from .api_views import _user_can_act_as_oic_for_equipment

    if not _user_can_act_as_oic_for_equipment(user, equipment):
        return "Only the Officer In-charge of this equipment can register requests for it."
    return None


def fallback_charge_profile(equipment):
    """Any active charge profile of the equipment: the booking needs one, though nothing is charged."""
    from .models import ChargeProfile

    return ChargeProfile.objects.filter(equipment=equipment, is_active=True).order_by("pk").first()


def no_charge():
    return Decimal("0.00"), [{"description": NO_CHARGE_NOTE, "amount": 0}]


def registered_requests(user, equipment):
    """Bookings this OIC registered for the equipment, newest first."""
    from .models import Booking, BookingEventType

    return (
        Booking.objects.filter(
            equipment=equipment,
            user=user,
            events__event_type=BookingEventType.CREATED,
            events__metadata__oic_registered=True,
        )
        .distinct()
        .prefetch_related("daily_slots")
        .order_by("-created_at")
    )


class NoChargeWallet:
    """Stands in for the wallet when an OIC (who has no wallet of their own) registers a request: nothing is
    charged, so nothing is read from or written to a wallet."""

    pk = None
    id = None
    balance = Decimal("0.00")
    user = None
    user_id = None

    def refresh_from_db(self, *args, **kwargs):
        return None
