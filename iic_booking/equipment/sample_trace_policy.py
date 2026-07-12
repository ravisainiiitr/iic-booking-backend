"""
Classify sample lifecycle for Booking Not Utilized vs operator-unavailable (full refund)
vs operator-absent disruption (refund/reschedule choice).

Booking Not Utilized applies only when the user never progressed beyond notifying that the
sample was sent (or has no trace rows). Once the sample is in the lab pipeline (forwarded,
accepted, in analysis/processing), use disruption policy — not Booking Not Utilized.

**Related Celery beat tasks** (Asia/Kolkata, see ``equipment.tasks``): ``check_booking_not_utilized`` (~20:00),
``auto_mark_operator_unavailable_after_booking_end`` (~20:30),
``auto_mark_operator_absent_disruption_after_booking_end`` (~20:35).

**Equipment admin** (Slot Configuration): ``booking_not_utilize_window_hours`` (manual UI gate),
``operator_unavailable_after_booking_end_hours``, ``operator_absent_disruption_after_booking_end_hours``.
"""

from __future__ import annotations

from .models import BookingSampleTrace, SampleTraceStatus

# Sample has entered lab / analysis workflow but is not finished (not COMPLETED/RETURNED/etc.).
SAMPLE_TRACE_IN_LAB_OR_ANALYSIS_STATUSES = frozenset(
    {
        SampleTraceStatus.FORWARDED_TO_LAB,
        SampleTraceStatus.SAMPLE_ACCEPTED,
        SampleTraceStatus.PROCESSING,
    }
)

# Auto full-refund "operator unavailable after booking end" must not run when the latest trace
# is one of these — disruption, lab pipeline, or awaiting user action on hold/reject.
OPERATOR_UNAVAILABLE_AUTO_REFUND_EXCLUDED_LATEST_STATUSES = frozenset(
    {
        SampleTraceStatus.HELD_AT_OFFICE,
        SampleTraceStatus.SAMPLE_REJECTED,
        SampleTraceStatus.FORWARDED_TO_LAB,
        SampleTraceStatus.SAMPLE_ACCEPTED,
        SampleTraceStatus.PROCESSING,
    }
)


def latest_sample_trace(booking_id: int) -> BookingSampleTrace | None:
    return (
        BookingSampleTrace.objects.filter(booking_id=booking_id)
        .order_by("-created_at", "-id")
        .first()
    )


def trace_allows_booking_not_utilized(booking_id: int) -> bool:
    """True if there is no trace or every row is SAMPLE_SENT only."""
    qs = BookingSampleTrace.objects.filter(booking_id=booking_id)
    if not qs.exists():
        return True
    return not qs.exclude(status=SampleTraceStatus.SAMPLE_SENT).exists()


def booking_not_utilized_blocked_detail(booking_id: int) -> str | None:
    """
    If marking Booking Not Utilized is blocked, return a short reason for API errors.
    None means allowed (caller still checks slot/time rules).
    """
    if trace_allows_booking_not_utilized(booking_id):
        return None
    latest = latest_sample_trace(booking_id)
    st = getattr(latest, "status", None) if latest else None
    if st in SAMPLE_TRACE_IN_LAB_OR_ANALYSIS_STATUSES:
        return (
            "Sample lifecycle shows the specimen in the lab or in analysis (forwarded, accepted, or processing). "
            "This is not Booking Not Utilized — use the operator disruption workflow (refund/reschedule policy)."
        )
    if st in (SampleTraceStatus.HELD_AT_OFFICE, SampleTraceStatus.SAMPLE_REJECTED):
        return (
            "Sample lifecycle is awaiting user action (held at office or sample rejected). "
            "Resolve that workflow instead of Booking Not Utilized."
        )
    return (
        "Sample lifecycle already has updates beyond 'Sample Sent'. "
        "Use Operator Unavailable, disruption handling, or complete the booking instead."
    )
