"""
User-facing booking references for Copilot replies.

Users only ever see the portal's display ID (``virtual_booking_id``, e.g. ``IICICPMS/MS202600001``;
fallback ``{equipment code}-{pk}``), never the numeric primary key. The pk stays in hrefs and action
payloads because the portal routes (``/my-bookings?booking=<pk>``) need it.
"""

from __future__ import annotations

import re
from typing import Any

# {department}{equipment code}{year}{5-digit sequence}[R]; the equipment code may contain "/", "-" or "_".
VIRTUAL_REF_RE = re.compile(r"(?<![A-Za-z0-9/_\-])([A-Za-z][A-Za-z0-9/_\-]*?(?:19|20)\d{7}R?)(?![A-Za-z0-9])")


def display_ref(booking) -> str:
    from iic_booking.communication.utils import booking_display_id_for_email

    return booking_display_id_for_email(booking)


def display_ref_for_id(booking_id: Any) -> str:
    """Display ID for a booking pk (internal lookups only; ownership is checked by the caller)."""
    from iic_booking.equipment.models import Booking

    try:
        b = Booking.objects.select_related("equipment").filter(pk=int(booking_id)).first()
    except (TypeError, ValueError):
        return ""
    return display_ref(b) if b is not None else ""


def find_virtual_ref(text: str) -> str | None:
    m = VIRTUAL_REF_RE.search(text or "")
    return m.group(1).upper() if m else None


def owned_booking_by_virtual_ref(user, ref: str, queryset=None):
    from iic_booking.equipment.models import Booking

    if not ref:
        return None
    qs = queryset if queryset is not None else Booking.objects.filter(user=user)
    return qs.filter(virtual_booking_id__iexact=ref.strip()).first()
