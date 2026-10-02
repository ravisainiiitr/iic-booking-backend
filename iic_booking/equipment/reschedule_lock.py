"""
Reschedule lock after the lab accepts the sample.

Once a booking's sample lifecycle reaches Sample Accepted (or any later stage), the booking user
and their supervisor can no longer reschedule it themselves. Staff who could reschedule before
(Admin, Officer In Charge incl. temporary OIC, Lab Operator, Department Admin with bookings.manage)
keep that ability.
"""

from __future__ import annotations

from .models import BookingSampleTrace, SampleTraceStatus

RESCHEDULE_LOCKED_SAMPLE_ACCEPTED = "reschedule_locked_sample_accepted"
RESCHEDULE_LOCKED_SAMPLE_ACCEPTED_MESSAGE = (
    "This booking can't be rescheduled because the lab has already accepted your sample. "
    "Please use 'Message the lab' to contact the Officer in Charge."
)

SAMPLE_ACCEPTED_OR_LATER_STATUSES = frozenset(
    {
        SampleTraceStatus.SAMPLE_ACCEPTED,
        SampleTraceStatus.PROCESSING,
        SampleTraceStatus.COMPLETED,
        SampleTraceStatus.RETURNED,
        SampleTraceStatus.ARCHIVED,
        SampleTraceStatus.DISPOSED,
    }
)


def sample_accepted_by_lab(booking) -> bool:
    cache = getattr(booking, "_prefetched_objects_cache", None) or {}
    if "sample_trace_events" in cache:
        return any(e.status in SAMPLE_ACCEPTED_OR_LATER_STATUSES for e in cache["sample_trace_events"])
    if not getattr(booking, "pk", None):
        return False
    return BookingSampleTrace.objects.filter(
        booking_id=booking.pk, status__in=SAMPLE_ACCEPTED_OR_LATER_STATUSES
    ).exists()


def sample_accepted_booking_ids(booking_ids) -> set[int]:
    ids = [int(b) for b in booking_ids if b is not None]
    if not ids:
        return set()
    return set(
        BookingSampleTrace.objects.filter(
            booking_id__in=ids, status__in=SAMPLE_ACCEPTED_OR_LATER_STATUSES
        ).values_list("booking_id", flat=True)
    )


def bypasses_sample_reschedule_lock(user) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    from .api_views import check_operator_permission

    return bool(check_operator_permission(user))


def reschedule_locked_for(user, booking) -> bool:
    return not bypasses_sample_reschedule_lock(user) and sample_accepted_by_lab(booking)


def reschedule_locked_payload() -> dict:
    return {
        "error": RESCHEDULE_LOCKED_SAMPLE_ACCEPTED_MESSAGE,
        "code": RESCHEDULE_LOCKED_SAMPLE_ACCEPTED,
    }
