"""
Per-equipment results deadline: "results within N working days (or N hours) after the slot or sample
receipt, whichever is later".

* The deadline exists only once the lab has received the sample: a ``SAMPLE_ACCEPTED`` sample-trace row
  (or any later stage). Walk-in equipment (no lead time, no collect deadline) never records receipt, so
  the sample counts as received at the slot.
  A booking in Processing status also counts as received.
* Anchor = max(last slot end, latest Sample Accepted time). A received booking without a Sample Accepted
  row (later stage recorded directly, or Processing status only) is anchored on the slot end. Working days skip Saturdays, Sundays
  and active ``Holiday`` rows; the deadline is the end (23:59:59 IST) of the N-th working day after
  the anchor day. Hours are clock hours after the anchor.
* An Admin / Officer In-Charge "Extend results deadline" on a booking (``operator_absent_hold_until``)
  moves that booking's deadline to the chosen time when it is later.
* Deadline passed (``is_results_overdue``) = still Pending / Booked / Processing after the deadline, the lab
  has the sample (see above), and the sample is not waiting for the user (held at office or rejected).
  The Results overdue list, filter, counters and reminders follow the separate per-equipment
  "Results overdue after (hours)" rule in ``results_overdue`` (``overdue_bookings`` delegates to it).
* Safeguard (replaces the fixed-hour Auto Operator Unavailable / Auto Operator Absent Disruption timers):
  when ``ResultsDeadlinePolicy.automation_enabled`` is on, bookings whose last slot ends at or after
  ``automation_since`` are acted on at their results deadline; earlier bookings keep the old timers.
  The safeguard uses ``safeguard_due_at``, never ``is_results_overdue``. A booking whose sample was never
  received keeps the slot-end timing (``unreceived_safeguard_due_at``), so the existing Operator
  Unavailable outcome for such samples is unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from typing import Optional

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

UNIT_WORKING_DAYS = "WORKING_DAYS"
UNIT_HOURS = "HOURS"
MAX_WORKING_DAYS = 60
MAX_HOURS = 720
DEADLINE_TIME = time(23, 59, 59)

MODE_RESULTS_DEADLINE = "results_deadline"
MODE_LEGACY = "legacy"


def _open_statuses():
    from .models import BookingStatus

    return (BookingStatus.PENDING, BookingStatus.BOOKED, BookingStatus.PROCESSING)


def _awaiting_user_stages():
    from .models import SampleTraceStatus

    return frozenset({SampleTraceStatus.HELD_AT_OFFICE, SampleTraceStatus.SAMPLE_REJECTED})


def _aware(dt):
    if dt is None:
        return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


# --- calendar -----------------------------------------------------------------------------------------------------


class WorkingCalendar:
    """Weekends plus active institute holidays, loaded once per date window (reuse one instance per request/job)."""

    WINDOW_DAYS = 400

    def __init__(self):
        self._start: Optional[date] = None
        self._end: Optional[date] = None
        self._holidays: set[date] = set()

    def _ensure(self, d: date) -> None:
        if self._start is not None and self._start <= d <= self._end:
            return
        from .models import Holiday

        start = d - timedelta(days=7)
        end = d + timedelta(days=self.WINDOW_DAYS)
        if self._start is not None:
            start = min(start, self._start)
            end = max(end, self._end)
        self._holidays = set(
            Holiday.objects.filter(is_active=True, date__gte=start, date__lte=end).values_list("date", flat=True)
        )
        self._start, self._end = start, end

    def is_working_day(self, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        self._ensure(d)
        return d not in self._holidays

    def add_working_days(self, d: date, n: int) -> date:
        """The n-th working day strictly after ``d`` (``d`` itself when n == 0)."""
        current = d
        remaining = max(0, int(n))
        while remaining > 0:
            current += timedelta(days=1)
            if self.is_working_day(current):
                remaining -= 1
        return current

    def working_days_between(self, start: date, end: date) -> int:
        """Working days after ``start`` up to and including ``end``."""
        n, d = 0, start
        while d < end:
            d += timedelta(days=1)
            if self.is_working_day(d):
                n += 1
        return n


# --- configuration ------------------------------------------------------------------------------------------------


def equipment_deadline_config(equipment) -> Optional[tuple[int, str]]:
    """(value, unit) or None when the equipment has no results deadline."""
    if equipment is None:
        return None
    value = int(getattr(equipment, "results_deadline_value", 0) or 0)
    if value <= 0:
        return None
    unit = getattr(equipment, "results_deadline_unit", None) or UNIT_WORKING_DAYS
    if unit not in (UNIT_WORKING_DAYS, UNIT_HOURS):
        unit = UNIT_WORKING_DAYS
    return value, unit


def deadline_label(value: int, unit: str) -> str:
    if unit == UNIT_HOURS:
        amount = f"{value} hour{'s' if value != 1 else ''}"
    else:
        amount = f"{value} working day{'s' if value != 1 else ''}"
    return f"within {amount} after the slot or sample receipt, whichever is later"


def validate_deadline(value, unit) -> tuple[Optional[int], Optional[str], dict]:
    """Shared validation for the OIC settings and admin APIs: (value, unit, errors)."""
    errors: dict = {}
    unit = str(unit or UNIT_WORKING_DAYS).strip().upper()
    if unit not in (UNIT_WORKING_DAYS, UNIT_HOURS):
        errors["results_deadline_unit"] = "Choose Working days or Hours."
        return None, None, errors
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors["results_deadline_value"] = "Enter a whole number."
        return None, unit, errors
    high = MAX_HOURS if unit == UNIT_HOURS else MAX_WORKING_DAYS
    if not 0 <= number <= high:
        errors["results_deadline_value"] = f"Enter a value between 0 and {high}."
        return None, unit, errors
    return number, unit, errors


def working_days_from_timer_hours(absent_hours, unavailable_hours) -> int:
    """Initial value from the old timers (same rule as migration 0222)."""
    hours = absent_hours or unavailable_hours or 0
    if hours <= 0:
        return 0
    calendar_days = math.ceil(hours / 24)
    return max(1, math.ceil(calendar_days * 5 / 7))


# --- deadlines ----------------------------------------------------------------------------------------------------


def compute_results_deadline(slot_end, value: int, unit: str, calendar: Optional[WorkingCalendar] = None):
    slot_end = _aware(slot_end)
    if slot_end is None or not value:
        return None
    if unit == UNIT_HOURS:
        return slot_end + timedelta(hours=int(value))
    calendar = calendar or WorkingCalendar()
    local_day = timezone.localtime(slot_end).date()
    due_day = calendar.add_working_days(local_day, int(value))
    return timezone.make_aware(datetime.combine(due_day, DEADLINE_TIME))


def booking_last_slot_end(booking):
    annotated = getattr(booking, "last_slot_end", None)
    if annotated is not None:
        return _aware(annotated)
    ends = [s.end_datetime for s in booking.daily_slots.all() if getattr(s, "end_datetime", None)]
    return _aware(max(ends)) if ends else None


def _hold_until(booking):
    return _aware(getattr(booking, "operator_absent_hold_until", None))


# --- sample receipt -----------------------------------------------------------------------------------------------

RECEIPT_SAMPLE_ACCEPTED = "sample_accepted"
RECEIPT_WALK_IN = "walk_in"
RECEIPT_NO_TIMESTAMP = "no_timestamp"


def received_statuses() -> frozenset:
    from .reschedule_lock import SAMPLE_ACCEPTED_OR_LATER_STATUSES

    return SAMPLE_ACCEPTED_OR_LATER_STATUSES


@dataclass(frozen=True)
class SampleReceipt:
    received: bool
    received_at: Optional[datetime] = None
    source: Optional[str] = None


def _receipt_from_events(events) -> tuple[bool, Optional[datetime]]:
    from .models import SampleTraceStatus

    statuses = received_statuses()
    received = any(e.status in statuses for e in events)
    accepted = [e.created_at for e in events if e.status == SampleTraceStatus.SAMPLE_ACCEPTED and e.created_at]
    return received, (_aware(max(accepted)) if accepted else None)


def booking_sample_receipt(booking) -> SampleReceipt:
    """
    Whether the lab has received the booking's sample, and when (latest Sample Accepted).

    Reads ``_sample_received`` / ``_sample_received_at`` when annotated (lists, jobs), else prefetched
    ``sample_trace_events``, else one query.
    """
    from django.db.models import Max

    from .models import BookingSampleTrace, BookingStatus, SampleTraceStatus
    from .sample_lifecycle_policy import equipment_is_walk_in_sample

    annotated = getattr(booking, "_sample_received", None)
    if annotated is not None:
        received, received_at = bool(annotated), _aware(getattr(booking, "_sample_received_at", None))
    else:
        cache = getattr(booking, "_prefetched_objects_cache", {}) or {}
        if "sample_trace_events" in cache:
            received, received_at = _receipt_from_events(cache["sample_trace_events"])
        elif getattr(booking, "pk", None):
            traces = BookingSampleTrace.objects.filter(booking_id=booking.pk)
            received = traces.filter(status__in=received_statuses()).exists()
            received_at = (
                _aware(traces.filter(status=SampleTraceStatus.SAMPLE_ACCEPTED).aggregate(at=Max("created_at"))["at"])
                if received
                else None
            )
        else:
            received, received_at = False, None
    if received:
        return SampleReceipt(True, received_at, RECEIPT_SAMPLE_ACCEPTED if received_at else RECEIPT_NO_TIMESTAMP)
    if getattr(booking, "status", None) == BookingStatus.PROCESSING:
        return SampleReceipt(True, None, RECEIPT_NO_TIMESTAMP)
    if equipment_is_walk_in_sample(getattr(booking, "equipment", None)):
        return SampleReceipt(True, None, RECEIPT_WALK_IN)
    return SampleReceipt(False)


def annotate_sample_receipt(queryset):
    """Adds ``_sample_received`` (bool) and ``_sample_received_at`` (latest Sample Accepted) to a Booking queryset."""
    from django.db.models import Exists, OuterRef, Subquery

    from .models import BookingSampleTrace, SampleTraceStatus

    return queryset.annotate(
        _sample_received=Exists(
            BookingSampleTrace.objects.filter(booking_id=OuterRef("pk"), status__in=received_statuses())
        ),
        _sample_received_at=Subquery(
            BookingSampleTrace.objects.filter(booking_id=OuterRef("pk"), status=SampleTraceStatus.SAMPLE_ACCEPTED)
            .order_by("-created_at", "-id")
            .values("created_at")[:1]
        ),
    )


def sample_received_q():
    """Booking filter: sample received (Sample Accepted or later, or Processing) or walk-in equipment. Needs ``annotate_sample_receipt``."""
    from django.db.models import Q

    from .models import BookingStatus
    from .sample_lifecycle_policy import walk_in_sample_equipment_q

    return Q(_sample_received=True) | Q(status=BookingStatus.PROCESSING) | walk_in_sample_equipment_q("equipment__")


def results_deadline_anchor(slot_end, receipt: SampleReceipt):
    """max(slot end, receipt time); the slot end when the receipt time is unknown or the sample came to the slot."""
    slot_end = _aware(slot_end)
    if slot_end is None or not receipt.received:
        return None
    if receipt.received_at is not None and receipt.received_at > slot_end:
        return receipt.received_at
    return slot_end


@dataclass
class BookingDeadline:
    value: int
    unit: str
    slot_end: datetime
    base_due_at: datetime
    due_at: datetime
    extended: bool
    anchor: Optional[datetime] = None
    received_at: Optional[datetime] = None
    receipt_source: Optional[str] = None

    @property
    def label(self) -> str:
        return deadline_label(self.value, self.unit)

    @property
    def counted_from_receipt(self) -> bool:
        return self.anchor is not None and self.anchor > self.slot_end


def booking_results_deadline(booking, calendar: Optional[WorkingCalendar] = None) -> Optional[BookingDeadline]:
    """The booking's results deadline, or None when the equipment has none or the sample is not received yet."""
    cfg = equipment_deadline_config(getattr(booking, "equipment", None))
    if not cfg:
        return None
    slot_end = booking_last_slot_end(booking)
    if slot_end is None:
        return None
    receipt = booking_sample_receipt(booking)
    anchor = results_deadline_anchor(slot_end, receipt)
    if anchor is None:
        return None
    value, unit = cfg
    base = compute_results_deadline(anchor, value, unit, calendar)
    hold = _hold_until(booking)
    due = max(base, hold) if hold else base
    return BookingDeadline(
        value=value, unit=unit, slot_end=slot_end, base_due_at=base, due_at=due, extended=due > base,
        anchor=anchor, received_at=receipt.received_at, receipt_source=receipt.source,
    )


def _latest_stage(booking) -> Optional[str]:
    annotated = getattr(booking, "_latest_stage", None)
    if annotated is not None:
        return annotated or None
    cache = getattr(booking, "_prefetched_objects_cache", {}) or {}
    if "sample_trace_events" in cache:
        events = sorted(cache["sample_trace_events"], key=lambda e: (e.created_at, e.id))
        return events[-1].status if events else None
    from .sample_trace_policy import latest_sample_trace

    latest = latest_sample_trace(booking.booking_id)
    return getattr(latest, "status", None)


def is_results_overdue(booking, deadline: Optional[BookingDeadline], now=None) -> bool:
    if deadline is None or booking.status not in _open_statuses():
        return False
    if (now or timezone.now()) <= deadline.due_at:
        return False
    return _latest_stage(booking) not in _awaiting_user_stages()


def overdue_label(due_at, now=None) -> str:
    delta = (now or timezone.now()) - due_at
    hours = max(int(delta.total_seconds() // 3600), 0)
    days, rem = divmod(hours, 24)
    if days:
        return f"{days} day{'s' if days != 1 else ''}" + (f" {rem} h" if rem else "")
    return f"{hours} h" if hours else "less than 1 h"


def due_display(deadline: BookingDeadline) -> str:
    local = timezone.localtime(deadline.due_at)
    if deadline.unit == UNIT_HOURS or deadline.extended:
        return local.strftime("%a %d %b %Y, %I:%M %p")
    return local.strftime("%a %d %b %Y")


def overdue_bookings(queryset, now=None) -> list[tuple]:
    """[(booking, ResultsDue)] whose results are overdue under the "Results overdue after (hours)" rule."""
    from .results_overdue import overdue_bookings as _overdue_bookings

    return _overdue_bookings(queryset, now)


def overdue_booking_ids(queryset, now=None) -> list[int]:
    return [b.booking_id for b, _d in overdue_bookings(queryset, now)]


# --- API payloads -------------------------------------------------------------------------------------------------


def viewer_is_staff(user) -> bool:
    from iic_booking.users.models.user_type import UserType

    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return getattr(user, "user_type", None) in (
        UserType.ADMIN,
        UserType.MANAGER,
        UserType.OPERATOR,
        UserType.DEPT_ADMIN,
    )


def booking_results_deadline_payload(booking, *, staff_view: bool, calendar=None, now=None) -> Optional[dict]:
    """Booking details / list field. Users only get it when the equipment shows the deadline to users."""
    equipment = getattr(booking, "equipment", None)
    visible = bool(getattr(equipment, "show_results_deadline_to_users", False))
    if not staff_view and not visible:
        return None
    deadline = booking_results_deadline(booking, calendar)
    if deadline is None:
        return None
    return {
        "value": deadline.value,
        "unit": deadline.unit,
        "label": deadline.label,
        "due_at": deadline.due_at.isoformat(),
        "due_display": due_display(deadline),
        "extended": deadline.extended,
        "overdue": is_results_overdue(booking, deadline, now) if staff_view else False,
        "visible_to_user": visible,
        "anchor_at": deadline.anchor.isoformat(),
        "sample_received_at": deadline.received_at.isoformat() if deadline.received_at else None,
        "receipt_source": deadline.receipt_source,
        "counted_from_receipt": deadline.counted_from_receipt,
    }


def preload_page(bookings) -> None:
    """Fill ``last_slot_end`` / ``_latest_stage`` / sample receipt on a page of bookings with two queries."""
    from django.db.models import Max

    from .models import BookingSampleTrace, DailySlot, SampleTraceStatus

    rows = [
        b for b in bookings
        if getattr(b, "booking_id", None) and equipment_deadline_config(getattr(b, "equipment", None))
    ]
    if not rows:
        return
    ids = [b.booking_id for b in rows]
    ends = dict(
        DailySlot.objects.filter(booking_id__in=ids)
        .values("booking_id")
        .annotate(end=Max("end_datetime"))
        .values_list("booking_id", "end")
    )
    latest: dict = {}
    received: set = set()
    accepted_at: dict = {}
    statuses = received_statuses()
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
    for b in rows:
        if getattr(b, "last_slot_end", None) is None:
            b.last_slot_end = ends.get(b.booking_id)
        if getattr(b, "_latest_stage", None) is None:
            b._latest_stage = latest.get(b.booking_id, "")
        if getattr(b, "_sample_received", None) is None:
            b._sample_received = b.booking_id in received
            b._sample_received_at = accepted_at.get(b.booking_id)


def public_equipment_deadline(equipment) -> Optional[dict]:
    """Catalog / policy dialog: only equipment whose OIC chose to show the deadline to users."""
    if not getattr(equipment, "show_results_deadline_to_users", False):
        return None
    cfg = equipment_deadline_config(equipment)
    if not cfg:
        return None
    value, unit = cfg
    return {"value": value, "unit": unit, "label": deadline_label(value, unit)}


def serialize_overdue_booking(booking, due, now=None, calendar=None) -> dict:
    """``due`` is a ``results_overdue.ResultsDue``; the equipment's results deadline (if any) is added for reference."""
    from iic_booking.communication.in_app import person_label
    from iic_booking.communication.utils import booking_display_id_for_email

    from .results_overdue import due_display as overdue_due_display

    equipment = booking.equipment
    deadline = booking_results_deadline(booking, calendar)
    return {
        "booking_id": booking.booking_id,
        "booking_ref": booking_display_id_for_email(booking) or str(booking.booking_id),
        "equipment_id": equipment.equipment_id,
        "equipment_name": equipment.name,
        "equipment_code": equipment.code,
        "user_name": person_label(booking.user),
        "status": booking.status,
        "slot_ended_at": due.slot_end.isoformat(),
        "anchor_at": due.anchor.isoformat(),
        "sample_received_at": due.received_at.isoformat() if due.received_at else None,
        "receipt_source": due.receipt_source,
        "due_at": due.due_at.isoformat(),
        "due_display": overdue_due_display(due.due_at),
        "overdue_after_hours": due.hours,
        "deadline_label": due.label,
        "extended": due.extended,
        "overdue_by": overdue_label(due.due_at, now),
        "results_deadline_display": due_display(deadline) if deadline else "",
        "link": f"/booking-management?expand={booking.booking_id}",
    }


def overdue_scope_equipment_ids(user) -> Optional[list[int]]:
    """None = all equipment (Main Admin); [] = nothing. OIC / temporary OIC / Lab in-charge see their equipment."""
    from iic_booking.users.models.user_type import UserType

    from .completion_reminders import awaiting_completion_equipment_ids

    if not getattr(user, "is_authenticated", False):
        return []
    if getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == UserType.ADMIN:
        return None
    return awaiting_completion_equipment_ids(user)


def results_overdue_for_user(user, now=None) -> list[tuple]:
    from .models import Booking

    ids = overdue_scope_equipment_ids(user)
    qs = Booking.objects.all()
    if ids is not None:
        if not ids:
            return []
        qs = qs.filter(equipment_id__in=ids)
    return overdue_bookings(qs, now)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def results_overdue_view(request):
    now = timezone.now()
    calendar = WorkingCalendar()
    rows = [serialize_overdue_booking(b, d, now, calendar) for b, d in results_overdue_for_user(request.user, now)]
    return Response({"count": len(rows), "bookings": rows}, status=status.HTTP_200_OK)


# --- safeguard automation -----------------------------------------------------------------------------------------


@dataclass
class AutomationState:
    enabled: bool
    since: Optional[datetime]


def automation_state() -> AutomationState:
    from .models import ResultsDeadlinePolicy

    row = ResultsDeadlinePolicy.objects.order_by("pk").first()
    if row is None or not row.automation_enabled or row.automation_since is None:
        return AutomationState(enabled=False, since=None)
    return AutomationState(enabled=True, since=_aware(row.automation_since))


def uses_results_deadline(slot_end, state: AutomationState) -> bool:
    return bool(state.enabled and state.since is not None and slot_end is not None and slot_end >= state.since)


def safeguard_due_at(booking, slot_end, *, legacy_hours, state: AutomationState, calendar=None):
    """
    (due_at, mode) for the scheduled safeguard jobs; due_at None means the safeguard is off for this booking.

    Results-deadline mode: the equipment's results deadline (or the booking's later extension); for a
    sample the lab never received, the same period counted from the slot end (``unreceived_safeguard_due_at``).
    Legacy mode: the old ``max(slot end, extension) + N hours``.
    """
    slot_end = _aware(slot_end)
    if uses_results_deadline(slot_end, state):
        if not booking_sample_receipt(booking).received:
            return unreceived_safeguard_due_at(booking, slot_end, calendar), MODE_RESULTS_DEADLINE
        deadline = booking_results_deadline(booking, calendar)
        return (deadline.due_at if deadline else None), MODE_RESULTS_DEADLINE
    hours = int(legacy_hours or 0)
    if hours <= 0:
        return None, MODE_LEGACY
    hold = _hold_until(booking)
    base = max(slot_end, hold) if hold else slot_end
    return base + timedelta(hours=hours), MODE_LEGACY


def unreceived_safeguard_due_at(booking, slot_end, calendar=None):
    """
    When the Operator Unavailable safeguard may act on a booking whose sample was never received: the
    equipment's results period counted from the slot end (or the later extension). Not a results
    deadline: it is never shown or emailed, and the booking is not listed as awaiting completion.
    """
    cfg = equipment_deadline_config(getattr(booking, "equipment", None))
    slot_end = _aware(slot_end)
    if not cfg or slot_end is None:
        return None
    value, unit = cfg
    base = compute_results_deadline(slot_end, value, unit, calendar)
    hold = _hold_until(booking)
    return max(base, hold) if hold else base


def set_automation(enabled: bool, *, user=None):
    from .models import ResultsDeadlinePolicy

    row = ResultsDeadlinePolicy.objects.order_by("pk").first() or ResultsDeadlinePolicy()
    row.automation_enabled = bool(enabled)
    if not enabled:
        row.automation_since = None
    row.updated_by = user
    row.save()
    return row


# --- dry run ------------------------------------------------------------------------------------------------------


def _safeguard_action_now(booking, slot_end, state, calendar, now) -> Optional[str]:
    """What the two scheduled jobs would do to this booking right now: 'operator_unavailable', 'operator_absent' or None."""
    from .models import BookingSampleTrace, SampleTraceStatus
    from .sample_trace_policy import (
        OPERATOR_UNAVAILABLE_AUTO_REFUND_EXCLUDED_LATEST_STATUSES,
        SAMPLE_TRACE_IN_LAB_OR_ANALYSIS_STATUSES,
    )

    equipment = booking.equipment
    traces = list(
        BookingSampleTrace.objects.filter(booking_id=booking.booking_id)
        .order_by("created_at", "id")
        .values_list("status", "created_at")
    )
    if not traces:
        return None
    statuses = {s for s, _ in traces}
    latest_status, latest_at = traces[-1]

    due, mode = safeguard_due_at(
        booking, slot_end, legacy_hours=equipment.operator_absent_disruption_after_booking_end_hours,
        state=state, calendar=calendar,
    )
    if due is not None and now >= due and latest_status in SAMPLE_TRACE_IN_LAB_OR_ANALYSIS_STATUSES:
        hours = int(equipment.operator_absent_disruption_after_booking_end_hours or 0)
        if mode == MODE_RESULTS_DEADLINE or now >= _aware(latest_at) + timedelta(hours=hours):
            return "operator_absent"

    due, _mode = safeguard_due_at(
        booking, slot_end, legacy_hours=equipment.operator_unavailable_after_booking_end_hours,
        state=state, calendar=calendar,
    )
    finished = {
        SampleTraceStatus.COMPLETED, SampleTraceStatus.RETURNED, SampleTraceStatus.ARCHIVED,
        SampleTraceStatus.DISPOSED, SampleTraceStatus.NOT_UTILIZED, SampleTraceStatus.OP_UNAVAILABLE,
    }
    if (
        due is not None
        and now >= due
        and statuses - {SampleTraceStatus.SAMPLE_SENT}
        and latest_status not in OPERATOR_UNAVAILABLE_AUTO_REFUND_EXCLUDED_LATEST_STATUSES
        and not statuses & finished
    ):
        return "operator_unavailable"
    return None


def dry_run(now=None) -> dict:
    """
    Read-only. Counts what the safeguard jobs would do now: with the current switch, if switched on now
    (window starts now, so past bookings stay on the old timers), and, for information only, if the
    results-deadline rule applied to every existing booking.
    """
    from django.db.models import Max

    from .models import Booking, BookingStatus, Equipment

    now = now or timezone.now()
    current = automation_state()
    if_enabled_now = AutomationState(enabled=True, since=now)
    everything = AutomationState(enabled=True, since=datetime(1970, 1, 1, tzinfo=dt_timezone.utc))
    calendar = WorkingCalendar()

    open_past = (
        Booking.objects.filter(status__in=[BookingStatus.PENDING, BookingStatus.BOOKED])
        .annotate(last_slot_end=Max("daily_slots__end_datetime"))
        .filter(last_slot_end__isnull=False, last_slot_end__lt=now)
        .select_related("equipment", "user")
    )
    counts = {"current": {}, "if_enabled_now": {}, "if_applied_to_all_existing": {}}
    acted_ids = {k: [] for k in counts}
    for booking in open_past:
        slot_end = _aware(booking.last_slot_end)
        for key, state in (("current", current), ("if_enabled_now", if_enabled_now), ("if_applied_to_all_existing", everything)):
            action = _safeguard_action_now(booking, slot_end, state, calendar, now)
            if action:
                counts[key][action] = counts[key].get(action, 0) + 1
                acted_ids[key].append(booking.booking_id)

    overdue = overdue_bookings(Booking.objects.all(), now)
    equipment_rows = [
        {
            "code": e.code,
            "results_deadline": deadline_label(e.results_deadline_value, e.results_deadline_unit)
            if e.results_deadline_value
            else "none",
            "shown_to_users": e.show_results_deadline_to_users,
            "old_timers_hours": (e.operator_unavailable_after_booking_end_hours, e.operator_absent_disruption_after_booking_end_hours),
        }
        for e in Equipment.objects.order_by("code").only(
            "code", "results_deadline_value", "results_deadline_unit", "show_results_deadline_to_users",
            "operator_unavailable_after_booking_end_hours", "operator_absent_disruption_after_booking_end_hours",
        )
    ]
    return {
        "now": now.isoformat(),
        "automation_enabled": current.enabled,
        "automation_since": current.since.isoformat() if current.since else None,
        "open_bookings_past_slot_end": open_past.count(),
        "would_act_now": counts["current"],
        "would_act_if_enabled_now": counts["if_enabled_now"],
        "would_act_if_applied_to_all_existing": counts["if_applied_to_all_existing"],
        "booking_ids_if_applied_to_all_existing": acted_ids["if_applied_to_all_existing"][:50],
        "results_overdue_now": len(overdue),
        "results_overdue_booking_ids": [b.booking_id for b, _d in overdue[:50]],
        "equipment": equipment_rows,
    }


def dry_run_report_text(now=None) -> str:
    data = dry_run(now)
    lines = [
        f"automation_enabled={data['automation_enabled']} since={data['automation_since']}",
        f"open (Pending/Booked) bookings past slot end={data['open_bookings_past_slot_end']}",
        f"safeguard would act now (current switch)={sum(data['would_act_now'].values())} {data['would_act_now']}",
        f"safeguard would act if switched on now={sum(data['would_act_if_enabled_now'].values())} {data['would_act_if_enabled_now']}",
        "if the results-deadline rule applied to all existing bookings (information only)="
        f"{sum(data['would_act_if_applied_to_all_existing'].values())} {data['would_act_if_applied_to_all_existing']} "
        f"ids={data['booking_ids_if_applied_to_all_existing']}",
        f"results overdue now={data['results_overdue_now']} ids={data['results_overdue_booking_ids']}",
        "equipment results deadlines:",
    ]
    for row in data["equipment"]:
        lines.append(
            f"  {row['code']:<14} {row['results_deadline']:<40} shown_to_users={row['shown_to_users']} "
            f"old_timers_h={row['old_timers_hours']}"
        )
    return "\n".join(lines)
