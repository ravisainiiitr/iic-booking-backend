"""Revenue bookings behind the Equipment Performance Report and the segments its rows drill down into.

Revenue = completed bookings with a slot (or a released slot range) in the report period, on the report's equipment
(modes included), leaving out test bookings, test equipment and bookings paid from a test account's wallet. The
report's revenue tables and every drill-down level (users in a segment, a user's bookings) are grouped from the same
rows (``revenue_rows``), so a drill-down always adds up to the figure it was opened from.
"""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional

from django.db.models import DateTimeField, Max, Min, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from iic_booking.users.user_categories import (
    CATEGORY_LABELS,
    EXTERNAL_CATEGORIES,
    category_for,
    category_label,
    normalize_code,
)

from .models import Booking, BookingSlotRange, BookingStatus, DailySlot

NO_DEPARTMENT = "none"
NOT_RECORDED = "none"
NOT_RECORDED_CATEGORY = "not_recorded"


def report_period(date_from: Optional[str], date_to: Optional[str]) -> tuple[date, date]:
    """``(start, end)`` of a report: defaults to this month; a bad ``date_to`` is today."""
    today = timezone.localdate()
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else today.replace(day=1)
    except ValueError:
        start = today.replace(day=1)
    if date_to:
        try:
            end = datetime.strptime(date_to, "%Y-%m-%d").date()
        except ValueError:
            end = today
    else:
        end = start.replace(day=monthrange(start.year, start.month)[1])
    return start, end


def bookings_in_report_range(start: date, end: date, eq_ids: Iterable[int]) -> QuerySet:
    """Bookings with a slot in [start, end] (cancelled / refunded ones by their released slot range); no test bookings."""
    from iic_booking.users.test_accounts import exclude_test_bookings

    # Subquery instead of a daily_slots join: a join yields one row per slot, which inflates grouped Count/Sum.
    slot_booking_ids = DailySlot.objects.filter(date__gte=start, date__lte=end, booking__isnull=False).values(
        "booking_id"
    )
    return exclude_test_bookings(
        Booking.objects.filter(equipment_id__in=list(eq_ids)).filter(
            Q(booking_id__in=slot_booking_ids)
            | Q(
                released_slot_range__start_datetime__date__lte=end,
                released_slot_range__end_datetime__date__gte=start,
            )
        )
    ).order_by()


def report_revenue_bookings(start: date, end: date, eq_ids: Iterable[int]) -> QuerySet:
    """Completed bookings in the period that count as revenue (test-wallet bookings left out)."""
    from iic_booking.users.test_accounts import exclude_test_revenue_bookings

    return exclude_test_revenue_bookings(
        bookings_in_report_range(start, end, eq_ids).filter(status=BookingStatus.COMPLETED)
    )


def slot_bounds_annotations() -> dict[str, Any]:
    """``slot_start`` / ``slot_end``: the booking's held slots, else its released slot range."""
    held = DailySlot.objects.filter(booking_id=OuterRef("pk")).order_by().values("booking_id")
    released = BookingSlotRange.objects.filter(booking_id=OuterRef("pk"))
    return {
        "slot_start": Coalesce(
            Subquery(held.annotate(s=Min("start_datetime")).values("s")[:1], output_field=DateTimeField()),
            Subquery(released.values("start_datetime")[:1], output_field=DateTimeField()),
        ),
        "slot_end": Coalesce(
            Subquery(held.annotate(e=Max("end_datetime")).values("e")[:1], output_field=DateTimeField()),
            Subquery(released.values("end_datetime")[:1], output_field=DateTimeField()),
        ),
    }


def row_category(code: str | None) -> str:
    """Category of a booking's user-type snapshot; ``not_recorded`` when the booking has none."""
    return category_for(code) if normalize_code(code) else NOT_RECORDED_CATEGORY


def row_category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, "Not recorded")


def is_external_snapshot(code: str | None) -> bool:
    return row_category(code) in EXTERNAL_CATEGORIES


def month_key(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        value = timezone.localtime(value) if timezone.is_aware(value) else value
        return value.strftime("%Y-%m")
    return value.strftime("%Y-%m")


def month_label(key: str) -> str:
    try:
        return datetime.strptime(key, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return "Date not recorded"


@dataclass
class RevenueRow:
    booking_id: int
    user_id: int
    snapshot: str
    department_id: Optional[int]
    department_name: str
    equipment_id: int
    family_id: int
    equipment_department_id: Optional[int]
    equipment_department_name: str
    charge: Decimal
    month: str

    @property
    def category(self) -> str:
        return row_category(self.snapshot)

    @property
    def external(self) -> bool:
        return is_external_snapshot(self.snapshot)


def revenue_rows(
    qs: QuerySet, mode_rollup: dict[int, int] | None = None, *, date_field: str | None = None
) -> list[RevenueRow]:
    """One row per revenue booking; ``family_id`` is the parent equipment for modes.

    ``month`` is taken from ``date_field`` when given (e.g. the finance report's ``effective_date``), else from the
    booking's first slot (falling back to its completion time).
    """
    rollup = mode_rollup or {}
    fields = [
        "booking_id",
        "user_id",
        "user_type_snapshot",
        "user__department_id",
        "user__department__name",
        "equipment_id",
        "equipment__internal_department_id",
        "equipment__internal_department__name",
        "total_charge",
        "completed_at",
    ]
    if date_field:
        fields.append(date_field)
    else:
        qs = qs.annotate(**slot_bounds_annotations())
        fields.append("slot_start")
    rows = []
    for r in qs.values(*fields).order_by("booking_id"):
        eid = r["equipment_id"]
        when = r[date_field] if date_field else (r["slot_start"] or r["completed_at"])
        rows.append(
            RevenueRow(
                booking_id=r["booking_id"],
                user_id=r["user_id"],
                snapshot=normalize_code(r["user_type_snapshot"]),
                department_id=r["user__department_id"],
                department_name=r["user__department__name"] or "",
                equipment_id=eid,
                family_id=rollup.get(eid, eid),
                equipment_department_id=r["equipment__internal_department_id"],
                equipment_department_name=r["equipment__internal_department__name"] or "",
                charge=Decimal(str(r["total_charge"] or 0)),
                month=month_key(when),
            )
        )
    return rows


@dataclass
class Bucket:
    key: Any
    count: int = 0
    total: Decimal = field(default_factory=lambda: Decimal("0"))
    user_ids: set = field(default_factory=set)

    def add(self, row: RevenueRow) -> None:
        self.count += 1
        self.total += row.charge
        self.user_ids.add(row.user_id)


def group(rows: Iterable[RevenueRow], key: Callable[[RevenueRow], Any]) -> list[Bucket]:
    """Buckets by ``key``, largest revenue first (ties by booking count)."""
    buckets: dict[Any, Bucket] = {}
    for row in rows:
        k = key(row)
        if k not in buckets:
            buckets[k] = Bucket(k)
        buckets[k].add(row)
    return sorted(buckets.values(), key=lambda b: (-b.total, -b.count, str(b.key)))


def money(value) -> float:
    return round(float(value or 0), 2)


# ---------------------------------------------------------------------------------------------- dimensions

DIMENSIONS = {
    "all": "All revenue",
    "internal": "Internal revenue",
    "external": "External revenue",
    "category": "User category",
    "user_type": "User type",
    "external_category": "External category",
    "department": "Department",
    "equipment_department": "Equipment department",
    "internal_department": "Internal revenue by equipment department",
    "equipment": "Equipment",
    "internal_equipment": "Internal revenue by equipment",
    "user": "User",
    "month": "Month",
    "oic": "Officer in Charge",
    "operator": "Lab Operator",
}


@dataclass
class SegmentContext:
    """What a dimension needs beyond the row: equipment families per OIC / operator, display names."""

    equipment_names: dict[int, str] = field(default_factory=dict)
    staff_equipment: dict[int, set[int]] = field(default_factory=dict)
    staff_names: dict[int, str] = field(default_factory=dict)
    user_names: dict[int, str] = field(default_factory=dict)


def _int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def segment_predicate(dimension: str, value: str, ctx: SegmentContext) -> Callable[[RevenueRow], bool]:
    """Row filter for a drill-down segment. Raises ``ValueError`` for an unknown dimension."""
    v = str(value or "").strip()
    if dimension == "all":
        return lambda r: True
    if dimension == "internal":
        return lambda r: not r.external
    if dimension == "external":
        return lambda r: r.external
    if dimension == "category":
        return lambda r: r.category == v.lower()
    if dimension == "user_type":
        code = "" if v.lower() == NOT_RECORDED else v.lower()
        return lambda r: r.snapshot == code
    if dimension == "external_category":
        return lambda r: r.external and r.category == v.lower()
    if dimension == "department":
        dept = None if v.lower() == NO_DEPARTMENT else _int(v)
        return lambda r: r.department_id == dept
    if dimension in ("equipment_department", "internal_department"):
        dept = None if v.lower() == NO_DEPARTMENT else _int(v)
        internal_only = dimension == "internal_department"
        return lambda r: r.equipment_department_id == dept and not (internal_only and r.external)
    if dimension == "equipment":
        eid = _int(v)
        return lambda r: r.family_id == eid
    if dimension == "internal_equipment":
        eid = _int(v)
        return lambda r: r.family_id == eid and not r.external
    if dimension == "user":
        uid = _int(v)
        return lambda r: r.user_id == uid
    if dimension == "month":
        return lambda r: r.month == ("" if v.lower() == NOT_RECORDED else v)
    if dimension in ("oic", "operator"):
        families = ctx.staff_equipment.get(_int(v) or 0, set())
        return lambda r: r.family_id in families
    raise ValueError(f"Unknown drill-down dimension: {dimension}")


def segment_label(dimension: str, value: str, ctx: SegmentContext) -> str:
    v = str(value or "").strip()
    if dimension in ("all", "internal", "external"):
        return DIMENSIONS[dimension]
    if dimension in ("category", "external_category"):
        return row_category_label(v.lower())
    if dimension == "user_type":
        return category_label("" if v.lower() == NOT_RECORDED else v)
    if dimension in ("department", "equipment_department", "internal_department"):
        if v.lower() == NO_DEPARTMENT:
            return "No department"
        from iic_booking.users.models import Department

        name = Department.objects.filter(pk=_int(v) or 0).values_list("name", flat=True).first() or "Department"
        return f"{name} (internal)" if dimension == "internal_department" else name
    if dimension == "internal_equipment":
        return f"{ctx.equipment_names.get(_int(v) or 0, 'Equipment')} (internal)"
    if dimension == "equipment":
        return ctx.equipment_names.get(_int(v) or 0, "Equipment")
    if dimension == "user":
        return ctx.user_names.get(_int(v) or 0) or "User"
    if dimension == "month":
        return month_label(v)
    if dimension in ("oic", "operator"):
        return ctx.staff_names.get(_int(v) or 0) or DIMENSIONS[dimension]
    return DIMENSIONS.get(dimension, dimension)
