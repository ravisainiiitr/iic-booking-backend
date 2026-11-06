"""Service levels: how quickly and reliably each instrument serves its users, for one period against the previous one.

Every booking counts in the period of its first slot, on its instrument (modes of a multi-mode instrument count under
the parent). Test data is excluded; a Department Administrator sees their department's equipment.

- Carried out: Completed, or Processing results.
- No-show rate: not utilized ÷ (carried out + not utilized).
- Cancellation rate: cancelled or refunded ÷ bookings in the period.
- Lab-stopped rate: under maintenance, operator unavailable or analysis not possible ÷ bookings in the period.
- Booking lead: median days from the booking being made to its first slot, over bookings carried out.
- Results turnaround: median hours from the end of the last slot to the first results notice. Results delivered
  before the slot ended count as 0. ``results_timed`` says how many bookings this rests on; instruments that do not
  deliver files have none, and that is shown as no figure, never as a fast one.
- Results awaited: Processing bookings with no results notice yet.
- Utilisation: the Reports utilization factor (``iic_booking.equipment.utilization``).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any

from django.db.models import DateTimeField, F, Min, OuterRef, Subquery
from django.db.models import Max
from django.db.models.functions import Coalesce
from django.utils import timezone

from iic_booking.equipment.utilization import UtilizationTally, utilization_period, utilization_scope

from .common import bounds, int_values, iso, multi, page_meta, page_params, period, scope_for, scope_payload, share

CARRIED_OUT = frozenset({"COMPLETED", "PROCESSING"})
NOT_UTILIZED = "BOOKING_NOT_UTILIZED"
CANCELLED = frozenset({"CANCELLED", "REFUNDED"})
LAB_STOPPED = frozenset({"UNDER_MAINTENANCE", "OTHER_DISRUPTION", "ABSENT"})
NOT_YET_BOOKINGS = frozenset({"PENDING", "PENDING_PAYMENT", "WAITLISTED"})
SORTS = {
    "name": lambda r: (r["name"] or "").lower(),
    "bookings": lambda r: r["bookings"],
    "lead": lambda r: r["lead_days"],
    "turnaround": lambda r: r["turnaround_hours"],
    "no_show": lambda r: r["no_show_rate"],
    "cancellation": lambda r: r["cancellation_rate"],
    "lab_stopped": lambda r: r["lab_stopped_rate"],
    "awaited": lambda r: r["results_awaited"],
    "rating": lambda r: r["rating"],
    "utilisation": lambda r: r["utilisation"],
}


class _Tally:
    """Counts and raw values for one instrument, or for everything in the period."""

    def __init__(self) -> None:
        self.bookings = 0
        self.carried_out = 0
        self.not_utilized = 0
        self.cancelled = 0
        self.lab_stopped = 0
        self.results_awaited = 0
        self.lead_days: list[float] = []
        self.turnaround_hours: list[float] = []
        self.ratings: list[int] = []

    def add(self, record: dict[str, Any]) -> None:
        state = record["status"]
        self.bookings += 1
        if state in CARRIED_OUT:
            self.carried_out += 1
            first = record["first_slot"]
            if record["created_at"] and first:
                self.lead_days.append(max(0.0, (first - record["created_at"]).total_seconds() / 86400))
            notified = record["results_available_notified_at"]
            if notified and record["last_slot_end"]:
                self.turnaround_hours.append(max(0.0, (notified - record["last_slot_end"]).total_seconds() / 3600))
            if state == "PROCESSING" and not notified:
                self.results_awaited += 1
        elif state == NOT_UTILIZED:
            self.not_utilized += 1
        elif state in CANCELLED:
            self.cancelled += 1
        elif state in LAB_STOPPED:
            self.lab_stopped += 1
        if record["rating"] is not None and not record["rating_removed"]:
            self.ratings.append(record["rating"])

    def figures(self) -> dict[str, Any]:
        return {
            "bookings": self.bookings,
            "carried_out": self.carried_out,
            "not_utilized": self.not_utilized,
            "no_show_rate": share(self.not_utilized, self.carried_out + self.not_utilized),
            "cancelled": self.cancelled,
            "cancellation_rate": share(self.cancelled, self.bookings),
            "lab_stopped": self.lab_stopped,
            "lab_stopped_rate": share(self.lab_stopped, self.bookings),
            "lead_days": round(median(self.lead_days), 1) if self.lead_days else None,
            "turnaround_hours": round(median(self.turnaround_hours), 1) if self.turnaround_hours else None,
            "results_timed": len(self.turnaround_hours),
            "results_awaited": self.results_awaited,
            "rating": round(sum(self.ratings) / len(self.ratings), 2) if self.ratings else None,
            "rated": len(self.ratings),
        }


def _booking_records(scope, expanded: list[int], start: date, end: date):
    from iic_booking.equipment.models import BookingSlotRange, DailySlot

    low, high = bounds(start, end)
    held_slots = DailySlot.objects.filter(booking_id=OuterRef("booking_id")).order_by().values("booking_id")
    return (
        scope.bookings()
        .filter(equipment_id__in=expanded)
        .exclude(status__in=NOT_YET_BOOKINGS)
        .order_by()
        .annotate(
            first_slot=Coalesce(
                Subquery(held_slots.annotate(s=Min("start_datetime")).values("s")[:1], output_field=DateTimeField()),
                Subquery(
                    BookingSlotRange.objects.filter(booking_id=OuterRef("booking_id")).values("start_datetime")[:1],
                    output_field=DateTimeField(),
                ),
            ),
            last_slot_end=Subquery(held_slots.annotate(e=Max("end_datetime")).values("e")[:1], output_field=DateTimeField()),
        )
        .filter(first_slot__gte=low, first_slot__lt=high)
        .values(
            "equipment_id",
            "status",
            "created_at",
            "first_slot",
            "last_slot_end",
            "results_available_notified_at",
            "rating",
            "rating_removed",
        )
    )


def _utilisation(ids: list[int], start: date, end: date):
    """(tallies by instrument, period, rollup of a mode to its parent, and the total) for the dates."""
    span = utilization_period(start, end)
    util = utilization_scope(ids, span.requested_start, end, period=span)
    tallies: dict[int, UtilizationTally] = util.tallies()
    for slot in util.slots():
        util.add(tallies, slot)
    total = UtilizationTally()
    for tally in tallies.values():
        total.merge(tally)
    return tallies, span, util.rollup, total


def _measure(scope, ids: list[int], start: date, end: date):
    tallies, span, rollup, usage_total = _utilisation(ids, start, end)
    expanded = list(set(ids) | set(rollup))
    by_equipment: dict[int, _Tally] = defaultdict(_Tally)
    total = _Tally()
    for record in _booking_records(scope, expanded, start, end):
        by_equipment[rollup.get(record["equipment_id"], record["equipment_id"])].add(record)
        total.add(record)
    return by_equipment, total, tallies, span, rollup, usage_total


def build_service_levels(user, params) -> dict[str, Any]:
    from .equipment import _base

    scope = scope_for(user, params)
    start, end = period(params)
    base = _base(scope)
    chosen = int_values(multi(params, "equipment"))
    if chosen:
        base = base.filter(pk__in=chosen)
    records = {
        r["pk"]: r
        for r in base.values("pk", "name", "code", "internal_department_id", dept_name=F("internal_department__name"))
    }
    ids = list(records)

    by_equipment, total, usage, span, rollup, usage_total = _measure(scope, ids, start, end)
    days = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    _p_rows, previous, _p_usage, _p_span, _p_rollup, previous_usage = _measure(scope, ids, previous_start, previous_end)

    rows = []
    for eid, record in records.items():
        if rollup.get(eid, eid) != eid:
            continue  # a mode: counted on its parent's row
        tally = by_equipment.get(eid) or _Tally()
        use = usage.get(eid)
        if not tally.bookings and not (use and use.slots):
            continue  # no slots and no bookings in the period: nothing to measure
        rows.append(
            {
                "equipment_id": eid,
                "name": record["name"] or "",
                "code": record["code"] or "",
                "department": record["dept_name"] or "",
                "utilisation": use.factor if use else None,
                **tally.figures(),
            }
        )

    sort = str(params.get("sort") or "-bookings")
    key = SORTS.get(sort.lstrip("-")) or SORTS["bookings"]
    known = [r for r in rows if key(r) is not None]
    unknown = [r for r in rows if key(r) is None]
    known.sort(key=key, reverse=sort.startswith("-"))
    unknown.sort(key=SORTS["name"])
    ordered = known + unknown  # an instrument with no figure never looks best or worst

    offset, size = page_params(params)
    out = {
        **scope_payload(scope),
        "generated_at": iso(timezone.now()),
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "previous_from": previous_start.isoformat(),
        "previous_to": previous_end.isoformat(),
        "utilisation_period_note": span.note(),
        "summary": {
            **total.figures(),
            "utilisation": usage_total.factor,
            "instruments": len(rows),
            "previous": {**previous.figures(), "utilisation": previous_usage.factor},
        },
        "results": ordered[offset : offset + size],
        **page_meta(len(ordered), offset, size),
    }
    if str(params.get("with_options") or "").strip().lower() in ("1", "true", "yes", "on"):
        out["options"] = {
            "equipment": [
                {"id": r["pk"], "name": r["name"] or "", "code": r["code"] or ""}
                for r in sorted(_base(scope).values("pk", "name", "code"), key=lambda r: (r["name"] or "").lower())
            ]
        }
    return out
