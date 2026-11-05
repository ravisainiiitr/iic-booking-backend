"""A booking is urgent when an approved urgent request produced it (``UrgentBookingRequest.hold_booking``).

A hold still waiting for its decision, or whose request was rejected or expired, is not urgent. External special
requests have their own workflow and are excluded by ``UrgentBookingRequest.objects``.
"""

from __future__ import annotations

from django.db.models import Exists, OuterRef

from .models import UrgentBookingRequest, UrgentBookingRequestStatus


def _approved():
    return UrgentBookingRequest.objects.filter(status=UrgentBookingRequestStatus.APPROVED)


def urgent_booking_annotation() -> Exists:
    return Exists(_approved().filter(hold_booking_id=OuterRef("pk")))


def booking_is_urgent(booking) -> bool:
    annotated = getattr(booking, "is_urgent", None)
    if annotated is not None:
        return bool(annotated)
    return _approved().filter(hold_booking_id=booking.pk).exists()


def urgent_filter_requested(params) -> bool:
    return str(params.get("urgent") or "").strip().lower() in ("1", "true", "yes")
