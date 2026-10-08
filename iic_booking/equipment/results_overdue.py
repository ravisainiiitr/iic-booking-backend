"""
When an open booking's results become overdue: the single rule behind every "Results overdue" / "Overdue by"
counter, the Results overdue list and filter, the staff-app count and the daily 9:00 AM completion reminder.

* Only once the lab has the sample (``results_deadline.booking_sample_receipt``: a Sample Accepted row or a later
  stage, a Processing booking, or walk-in equipment, whose sample counts as received at the slot).
* Anchor = the later of the booking end (last slot end) and the sample receipt (latest Sample Accepted) plus the
  booked time (sum of the booked slots). Without a receipt time (walk-in, or a later stage recorded directly) the
  anchor is the booking end.
* Results due at = anchor + ``Equipment.results_overdue_after_hours`` (default 24), or the later time an Admin /
  OIC chose with Extend results deadline (``operator_absent_hold_until``).
* Overdue = still Pending / Booked / Processing at or after that time, and the sample is not waiting for the user
  (held at office or rejected).

The equipment's separate results deadline (``results_deadline``: N working days or hours, the user-facing
"Results expected by" date and the automatic safeguard) is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from django.utils import timezone

from .results_deadline import (
    _awaiting_user_stages,
    _aware,
    _hold_until,
    _latest_stage,
    _open_statuses,
    booking_last_slot_end,
    booking_sample_receipt,
    overdue_label,
    received_statuses,
)

DEFAULT_HOURS = 24
MIN_HOURS = 1
MAX_HOURS = 720
_CHUNK = 500


def equipment_overdue_hours(equipment) -> int:
    try:
        hours = int(getattr(equipment, "results_overdue_after_hours", None) or DEFAULT_HOURS)
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS
    return min(max(hours, MIN_HOURS), MAX_HOURS)


def validate_overdue_hours(value) -> tuple[Optional[int], Optional[str]]:
    try:
        hours = int(str(value).strip())
    except (TypeError, ValueError):
        return None, "Enter a whole number of hours."
    if not MIN_HOURS <= hours <= MAX_HOURS:
        return None, f"Enter a value between {MIN_HOURS} and {MAX_HOURS} hours."
    return hours, None


def rule_label(hours: int) -> str:
    return (
        f"{hours} hour{'s' if hours != 1 else ''} after the booking end, or after the sample receipt plus the "
        "booked time if that is later"
    )


def booking_booked_duration(booking) -> timedelta:
    """Sum of the booked slot lengths (``_booked_seconds`` when preloaded)."""
    seconds = getattr(booking, "_booked_seconds", None)
    if seconds is not None:
        return timedelta(seconds=float(seconds))
    cache = getattr(booking, "_prefetched_objects_cache", {}) or {}
    if "daily_slots" in cache:
        slots = [(s.start_datetime, s.end_datetime) for s in cache["daily_slots"]]
    elif getattr(booking, "pk", None):
        from .models import DailySlot

        slots = list(DailySlot.objects.filter(booking_id=booking.pk).values_list("start_datetime", "end_datetime"))
    else:
        slots = []
    total = sum(((e - s).total_seconds() for s, e in slots if s and e and e > s), 0.0)
    return timedelta(seconds=total)


@dataclass(frozen=True)
class ResultsDue:
    hours: int
    slot_end: datetime
    booked: timedelta
    received_at: Optional[datetime]
    receipt_source: Optional[str]
    anchor: datetime
    base_due_at: datetime
    due_at: datetime

    @property
    def extended(self) -> bool:
        return self.due_at > self.base_due_at

    @property
    def counted_from_receipt(self) -> bool:
        return self.anchor > self.slot_end

    @property
    def label(self) -> str:
        return rule_label(self.hours)


def results_anchor(slot_end, received_at, booked: timedelta):
    slot_end = _aware(slot_end)
    received_at = _aware(received_at)
    if received_at is not None and received_at + booked > slot_end:
        return received_at + booked
    return slot_end


def booking_results_due(booking) -> Optional[ResultsDue]:
    """None for a booking without slots or whose sample the lab has not received."""
    slot_end = booking_last_slot_end(booking)
    if slot_end is None:
        return None
    receipt = booking_sample_receipt(booking)
    if not receipt.received:
        return None
    booked = booking_booked_duration(booking)
    anchor = results_anchor(slot_end, receipt.received_at, booked)
    hours = equipment_overdue_hours(getattr(booking, "equipment", None))
    base = anchor + timedelta(hours=hours)
    hold = _hold_until(booking)
    return ResultsDue(
        hours=hours,
        slot_end=slot_end,
        booked=booked,
        received_at=receipt.received_at,
        receipt_source=receipt.source,
        anchor=anchor,
        base_due_at=base,
        due_at=max(base, hold) if hold else base,
    )


def results_due_at(booking) -> Optional[datetime]:
    due = booking_results_due(booking)
    return due.due_at if due else None


def waiting_for_user(booking) -> bool:
    return _latest_stage(booking) in _awaiting_user_stages()


def is_results_overdue(booking, due: Optional[ResultsDue], now=None) -> bool:
    if due is None or getattr(booking, "status", None) not in _open_statuses():
        return False
    if (now or timezone.now()) < due.due_at:
        return False
    return not waiting_for_user(booking)


def due_display(due_at) -> str:
    return timezone.localtime(due_at).strftime("%a %d %b %Y, %I:%M %p")


def overdue_by(due: ResultsDue, now=None) -> str:
    return overdue_label(due.due_at, now)


# --- batch loading ------------------------------------------------------------------------------------------------


def preload(bookings: Iterable) -> None:
    """Fill ``last_slot_end``, ``_booked_seconds``, ``_latest_stage`` and the sample receipt with two queries per chunk."""
    from .models import BookingSampleTrace, DailySlot, SampleTraceStatus

    rows = [b for b in bookings if getattr(b, "booking_id", None)]
    statuses = received_statuses()
    for i in range(0, len(rows), _CHUNK):
        chunk = rows[i : i + _CHUNK]
        ids = [b.booking_id for b in chunk]
        ends: dict = {}
        booked: dict = {}
        for booking_id, start, end in DailySlot.objects.filter(booking_id__in=ids).values_list(
            "booking_id", "start_datetime", "end_datetime"
        ):
            if end and (booking_id not in ends or end > ends[booking_id]):
                ends[booking_id] = end
            if start and end and end > start:
                booked[booking_id] = booked.get(booking_id, 0.0) + (end - start).total_seconds()
        latest: dict = {}
        received: set = set()
        accepted_at: dict = {}
        for booking_id, status, created_at in (
            BookingSampleTrace.objects.filter(booking_id__in=ids)
            .order_by("booking_id", "created_at", "id")
            .values_list("booking_id", "status", "created_at")
        ):
            latest[booking_id] = status
            if status in statuses:
                received.add(booking_id)
            if status == SampleTraceStatus.SAMPLE_ACCEPTED:
                accepted_at[booking_id] = created_at
        for b in chunk:
            if getattr(b, "last_slot_end", None) is None:
                b.last_slot_end = ends.get(b.booking_id)
            if getattr(b, "_booked_seconds", None) is None:
                b._booked_seconds = booked.get(b.booking_id, 0.0)
            if getattr(b, "_latest_stage", None) is None:
                b._latest_stage = latest.get(b.booking_id, "")
            if getattr(b, "_sample_received", None) is None:
                b._sample_received = b.booking_id in received
                b._sample_received_at = accepted_at.get(b.booking_id)


def overdue_bookings(queryset, now=None) -> list[tuple]:
    """[(booking, ResultsDue)] for bookings in ``queryset`` whose results are overdue, oldest due time first."""
    from django.db.models import Max

    from .results_deadline import annotate_sample_receipt, sample_received_q

    now = now or timezone.now()
    candidates = list(
        annotate_sample_receipt(queryset.filter(status__in=_open_statuses()))
        .annotate(last_slot_end=Max("daily_slots__end_datetime"))
        .filter(last_slot_end__isnull=False, last_slot_end__lte=now - timedelta(hours=MIN_HOURS))
        .filter(sample_received_q())
        .select_related("equipment", "user")
        .order_by()
    )
    preload(candidates)
    rows = []
    for booking in candidates:
        due = booking_results_due(booking)
        if is_results_overdue(booking, due, now):
            rows.append((booking, due))
    rows.sort(key=lambda r: (r[1].due_at, r[0].booking_id))
    return rows


def overdue_booking_ids(queryset, now=None) -> list[int]:
    return [b.booking_id for b, _d in overdue_bookings(queryset, now)]


# --- API payloads -------------------------------------------------------------------------------------------------


def booking_results_overdue_payload(booking, *, staff_view: bool, now=None) -> Optional[dict]:
    """Booking details / list: staff always; the booking user only when the equipment shows the countdown."""
    if getattr(booking, "status", None) not in _open_statuses():
        return None
    equipment = getattr(booking, "equipment", None)
    visible = bool(getattr(equipment, "show_results_countdown_to_users", False))
    if not staff_view and not visible:
        return None
    due = booking_results_due(booking)
    if due is None:
        return None
    now = now or timezone.now()
    overdue = is_results_overdue(booking, due, now)
    return {
        "hours": due.hours,
        "label": due.label,
        "due_at": due.due_at.isoformat(),
        "due_display": due_display(due.due_at),
        "overdue": overdue,
        "overdue_by": overdue_by(due, now) if overdue else None,
        "waiting_for_user": waiting_for_user(booking),
        "extended": due.extended,
        "anchor_at": due.anchor.isoformat(),
        "slot_end_at": due.slot_end.isoformat(),
        "sample_received_at": due.received_at.isoformat() if due.received_at else None,
        "booked_minutes": int(due.booked.total_seconds() // 60),
        "counted_from_receipt": due.counted_from_receipt,
        "visible_to_user": visible,
    }
