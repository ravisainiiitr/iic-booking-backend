"""Links to a booking's details page on the portal, for exports and emails.

Staff open the booking on View Booking (``/booking-management?expand=<pk>``); everyone else opens it on
My Bookings (``/my-bookings?booking=<Booking ID>``), the same links the notifications use. The page itself
checks access. Absolute URLs use ``settings.FRONTEND_URL`` and are empty when it is not configured.
"""

from __future__ import annotations

from urllib.parse import quote

from iic_booking.communication.utils import get_frontend_absolute_url


def booking_detail_path(*, pk=None, display_id: str = "", staff: bool) -> str:
    if staff:
        return f"/booking-management?expand={int(pk)}" if pk else ""
    display_id = str(display_id or "").strip()
    if display_id:
        return f"/my-bookings?booking={quote(display_id, safe='')}"
    return ""


def booking_detail_url(*, pk=None, display_id: str = "", staff: bool) -> str:
    path = booking_detail_path(pk=pk, display_id=display_id, staff=staff)
    return get_frontend_absolute_url(path) if path else ""


def booking_url_for(user, booking) -> str:
    """Absolute link to ``booking`` for ``user`` (View Booking for staff, My Bookings otherwise)."""
    from iic_booking.communication.utils import booking_display_id_for_email

    from .results_deadline import viewer_is_staff

    if booking is None:
        return ""
    return booking_detail_url(
        pk=getattr(booking, "pk", None),
        display_id=booking_display_id_for_email(booking),
        staff=viewer_is_staff(user),
    )
