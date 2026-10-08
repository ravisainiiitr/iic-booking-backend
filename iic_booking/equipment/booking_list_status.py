"""
Display status and default order of the booking lists (View Booking, My Bookings, their exports, booking
details, reports breakdown, staff slot calendars).

Two statuses are derived from the sample lifecycle, never stored, so charges, cancellation, refunds,
reschedule rules, quotas, reminders and calendars keep using the stored ``Booking.status``:

* Pending (``RESULTS_PENDING``): an open booking (stored Pending / Booked / Processing) whose sample the lab
  has: a Sample Accepted row or a later stage, a Processing booking, or walk-in equipment once the last slot
  has ended. Not while the sample is waiting for the user (held at office or rejected).
* Result Overdue (``RESULT_OVERDUE``): such a booking at or after its results due time
  (``results_overdue.results_due_at``, the same rule as the Results overdue list and counters). Users see
  it only when the equipment shows the results countdown to users; otherwise it stays Pending for them.

The stored ``PENDING`` status ("request received, waiting for confirmation") is unrelated, hence the key
``RESULTS_PENDING``.

Default order (``ordering=default``): groups 1-9 below; groups 1-4 oldest slot start first, groups 5-9 most
recent first. Result Overdue is ordered by due time first. Ties: newest booking first.
"""

from __future__ import annotations

from typing import Optional

from django.db.models import (
    Case,
    DateTimeField,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import BookingSampleTrace, BookingSlotRange, BookingStatus, DailySlot

RESULTS_PENDING = "RESULTS_PENDING"
RESULT_OVERDUE = "RESULT_OVERDUE"
DERIVED_LABELS = {RESULTS_PENDING: "Pending", RESULT_OVERDUE: "Result Overdue"}
LIST_STATUS_VALUES = frozenset(BookingStatus.values) | frozenset(DERIVED_LABELS)
DEFAULT_ORDERING = "default"

GROUP_RESULT_OVERDUE = 1
GROUP_RESULTS_PENDING = 2
GROUP_BOOKED = 3
GROUP_AWAITING_CHOICE = 4
GROUP_OPERATOR_UNAVAILABLE = 5
GROUP_NOT_UTILIZED = 6
GROUP_CANCELLED_REFUNDED = 7
GROUP_COMPLETED = 8
GROUP_OTHER = 9
ASCENDING_GROUPS_MAX = GROUP_AWAITING_CHOICE

STATUS_GROUPS = {
    RESULT_OVERDUE: GROUP_RESULT_OVERDUE,
    RESULTS_PENDING: GROUP_RESULTS_PENDING,
    BookingStatus.BOOKED: GROUP_BOOKED,
    BookingStatus.DISRUPTION_PENDING: GROUP_AWAITING_CHOICE,
    BookingStatus.ABSENT: GROUP_OPERATOR_UNAVAILABLE,
    BookingStatus.BOOKING_NOT_UTILIZED: GROUP_NOT_UTILIZED,
    BookingStatus.CANCELLED: GROUP_CANCELLED_REFUNDED,
    BookingStatus.REFUNDED: GROUP_CANCELLED_REFUNDED,
    BookingStatus.COMPLETED: GROUP_COMPLETED,
}


def status_group(list_status: str) -> int:
    return STATUS_GROUPS.get(list_status, GROUP_OTHER)


def _open_statuses():
    from .results_deadline import _open_statuses as open_statuses

    return open_statuses()


def _awaiting_user_stages():
    from .results_deadline import _awaiting_user_stages as stages

    return stages()


def label_for(list_status: str, booking=None) -> str:
    if list_status in DERIVED_LABELS:
        return DERIVED_LABELS[list_status]
    if booking is not None and list_status == getattr(booking, "status", None):
        from .serializers import _booking_status_display

        return _booking_status_display(booking)
    return str(dict(BookingStatus.choices).get(list_status, list_status))


# --- one booking (Python; same rule as the SQL annotation below) ---------------------------------------------------


def _overdue_visible(booking, staff_view: bool) -> bool:
    return staff_view or bool(getattr(getattr(booking, "equipment", None), "show_results_countdown_to_users", False))


def compute_list_status(booking, *, staff_view: bool, now=None) -> str:
    from .results_deadline import RECEIPT_WALK_IN, booking_last_slot_end, booking_sample_receipt
    from .results_overdue import booking_results_due, is_results_overdue, waiting_for_user

    status = getattr(booking, "status", None)
    if status not in _open_statuses():
        return status
    now = now or timezone.now()
    receipt = booking_sample_receipt(booking)
    if not receipt.received:
        return status
    if receipt.source == RECEIPT_WALK_IN:
        end = booking_last_slot_end(booking)
        if end is None or end > now:
            return status
    if waiting_for_user(booking):
        return status
    if _overdue_visible(booking, staff_view) and is_results_overdue(booking, booking_results_due(booking), now):
        return RESULT_OVERDUE
    return RESULTS_PENDING


def booking_list_status(booking, *, staff_view: bool, now=None) -> str:
    """``_list_status`` when the queryset was annotated, else computed."""
    annotated = getattr(booking, "_list_status", None)
    if annotated:
        return annotated
    return compute_list_status(booking, staff_view=staff_view, now=now)


# --- querysets ---------------------------------------------------------------------------------------------------


def _latest_stage():
    return Subquery(
        BookingSampleTrace.objects.filter(booking_id=OuterRef("pk")).order_by("-created_at", "-id").values("status")[:1]
    )


def _last_slot_end():
    return Subquery(
        DailySlot.objects.filter(booking_id=OuterRef("pk")).order_by("-end_datetime").values("end_datetime")[:1]
    )


def _first_slot_start():
    return Coalesce(
        Subquery(
            DailySlot.objects.filter(booking_id=OuterRef("pk")).order_by("start_datetime").values("start_datetime")[:1]
        ),
        Subquery(BookingSlotRange.objects.filter(booking_id=OuterRef("pk")).values("start_datetime")[:1]),
    )


def _overdue_positions(queryset, *, staff_view: bool, now) -> dict[int, int]:
    """{booking_id: rank of its due time} for the overdue bookings in ``queryset`` (equal due times share a rank)."""
    from .models import Booking
    from .results_overdue import overdue_bookings

    scope = Booking.objects.filter(
        booking_id__in=queryset.order_by().filter(status__in=_open_statuses()).values("booking_id")
    )
    if not staff_view:
        scope = scope.filter(equipment__show_results_countdown_to_users=True)
    positions: dict[int, int] = {}
    rank, last_due = -1, None
    for booking, due in overdue_bookings(scope, now):
        if due.due_at != last_due:
            rank, last_due = rank + 1, due.due_at
        positions[booking.booking_id] = rank
    return positions


def annotate_list_status(queryset, *, staff_view: bool = True, now=None):
    """
    Adds ``_list_status`` (stored status, or RESULTS_PENDING / RESULT_OVERDUE), ``_list_group`` (1-9) and
    ``_list_overdue_rank`` (due-time rank inside Result Overdue, else NULL).
    """
    from .sample_lifecycle_policy import walk_in_sample_equipment_q

    now = now or timezone.now()
    positions = _overdue_positions(queryset, staff_view=staff_view, now=now)
    queryset = queryset.annotate(
        _ls_received=Exists(
            BookingSampleTrace.objects.filter(booking_id=OuterRef("pk"), status__in=_received_statuses())
        ),
        _ls_stage=_latest_stage(),
        _ls_last_end=_last_slot_end(),
    )
    pending = (
        Q(status__in=_open_statuses())
        & (
            Q(_ls_received=True)
            | Q(status=BookingStatus.PROCESSING)
            | (walk_in_sample_equipment_q("equipment__") & Q(_ls_last_end__lte=now))
        )
        & (Q(_ls_stage__isnull=True) | ~Q(_ls_stage__in=list(_awaiting_user_stages())))
    )
    whens = [When(booking_id__in=list(positions), then=Value(RESULT_OVERDUE))] if positions else []
    queryset = queryset.annotate(
        _list_status=Case(*whens, When(pending, then=Value(RESULTS_PENDING)), default=F("status"))
    )
    group_whens = [
        When(_list_status=key, then=Value(group)) for key, group in STATUS_GROUPS.items()
    ]
    rank_whens = [When(booking_id=pk, then=Value(rank)) for pk, rank in positions.items()]
    return queryset.annotate(
        _list_group=Case(*group_whens, default=Value(GROUP_OTHER), output_field=IntegerField()),
        _list_overdue_rank=Case(*rank_whens, default=Value(None), output_field=IntegerField())
        if rank_whens
        else Value(None, output_field=IntegerField()),
    )


def _received_statuses():
    from .results_deadline import received_statuses

    return list(received_statuses())


def filter_list_status(queryset, list_status: str):
    """Needs ``annotate_list_status``."""
    return queryset.filter(_list_status=list_status)


def apply_default_order(queryset):
    """Needs ``annotate_list_status``. Groups 1-4 oldest slot start first, 5-9 most recent first."""
    queryset = queryset.annotate(_list_start=_first_slot_start())
    queryset = queryset.annotate(
        _list_start_asc=Case(
            When(_list_group__lte=ASCENDING_GROUPS_MAX, then=F("_list_start")), output_field=DateTimeField()
        ),
        _list_start_desc=Case(
            When(_list_group__gt=ASCENDING_GROUPS_MAX, then=F("_list_start")), output_field=DateTimeField()
        ),
    )
    return queryset.order_by(
        "_list_group",
        F("_list_overdue_rank").asc(nulls_last=True),
        F("_list_start_asc").asc(nulls_last=True),
        F("_list_start_desc").desc(nulls_last=True),
        "-booking_id",
    )


def list_status_map(booking_ids, *, staff_view: bool, now=None) -> dict[int, str]:
    """{booking_id: list status} for open bookings among ``booking_ids`` (others keep their stored status)."""
    from .models import Booking

    ids = sorted({int(i) for i in booking_ids if i})
    if not ids:
        return {}
    qs = annotate_list_status(
        Booking.objects.filter(booking_id__in=ids, status__in=_open_statuses()), staff_view=staff_view, now=now
    )
    return dict(qs.values_list("booking_id", "_list_status"))


def list_status_counts(queryset, *, staff_view: bool, now=None) -> dict[str, int]:
    from django.db.models import Count

    rows = (
        annotate_list_status(queryset.order_by(), staff_view=staff_view, now=now)
        .values("_list_status")
        .annotate(n=Count("pk"))
        .order_by()
    )
    counts: dict[str, int] = {}
    for row in rows:
        key = row["_list_status"] or "UNKNOWN"
        counts[key] = counts.get(key, 0) + int(row["n"] or 0)
    return counts


def staff_view_for(user) -> bool:
    from .results_deadline import viewer_is_staff

    return viewer_is_staff(user)


def parse_list_status(value: Optional[str]) -> Optional[str]:
    """Upper-cased ``list_status`` param; None for empty / all. ``RESULTS_OVERDUE`` (old filter name) is Result Overdue."""
    raw = (value or "").strip().upper()
    if not raw or raw == "ALL":
        return None
    return RESULT_OVERDUE if raw == "RESULTS_OVERDUE" else raw
