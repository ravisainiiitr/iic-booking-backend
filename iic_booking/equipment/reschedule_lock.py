"""
Reschedule / cancel lock after the lab accepts the sample.

Once a booking's sample lifecycle reaches Sample Accepted (or any later stage), the booking user
and their supervisor can no longer reschedule or cancel it themselves. Staff who could before
(Admin, Officer In Charge incl. temporary OIC, Lab Operator, Department Admin with bookings.manage)
keep that ability. A lab-flagged disruption (awaiting the user's choice, or under the maintenance
disruption policy) keeps the user's cancel / reschedule choice.
"""

from __future__ import annotations

from .models import BookingSampleTrace, SampleTraceStatus

RESCHEDULE_LOCKED_SAMPLE_ACCEPTED = "reschedule_locked_sample_accepted"
RESCHEDULE_LOCKED_SAMPLE_ACCEPTED_MESSAGE = (
    "This booking can't be rescheduled because the lab has already accepted your sample. "
    "Please use 'Message the lab' to contact the Officer in Charge."
)
CANCEL_LOCKED_SAMPLE_ACCEPTED = "cancel_locked_sample_accepted"
CANCEL_LOCKED_SAMPLE_ACCEPTED_MESSAGE = (
    "This booking can't be cancelled because the lab has already accepted your sample. "
    "Please use 'Message the lab' to contact the Officer in Charge."
)
CANCEL_OWNER_ONLY = "cancel_owner_only"
CANCEL_OWNER_ONLY_MESSAGE = "Only the booking user can cancel this booking."
RESCHEDULE_OWNER_ONLY = "reschedule_owner_only"
RESCHEDULE_OWNER_ONLY_MESSAGE = "Only the booking user can reschedule this booking."

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


def lab_disruption_active(booking) -> bool:
    from .models import BookingStatus

    return booking.status == BookingStatus.DISRUPTION_PENDING or bool(
        getattr(booking, "maintenance_disruption_flag", False)
    )


def bypasses_sample_reschedule_lock(user) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    from .api_views import check_operator_permission

    return bool(check_operator_permission(user))


bypasses_sample_accepted_lock = bypasses_sample_reschedule_lock


def user_changes_locked_for(user, booking, *, accepted: bool | None = None) -> bool:
    """True when `user` may no longer reschedule or cancel `booking` because the lab accepted the sample."""
    if bypasses_sample_accepted_lock(user) or lab_disruption_active(booking):
        return False
    return sample_accepted_by_lab(booking) if accepted is None else accepted


def reschedule_locked_for(user, booking) -> bool:
    return user_changes_locked_for(user, booking)


def cancel_locked_for(user, booking) -> bool:
    return user_changes_locked_for(user, booking)


def reschedule_locked_payload() -> dict:
    return {
        "error": RESCHEDULE_LOCKED_SAMPLE_ACCEPTED_MESSAGE,
        "code": RESCHEDULE_LOCKED_SAMPLE_ACCEPTED,
    }


def cancel_locked_payload() -> dict:
    return {
        "error": CANCEL_LOCKED_SAMPLE_ACCEPTED_MESSAGE,
        "code": CANCEL_LOCKED_SAMPLE_ACCEPTED,
    }
