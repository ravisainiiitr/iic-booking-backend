"""Drill-downs behind the Reports & Statistics revenue figures: segment → users → a user's bookings.

A segment is one row or card of a report (``dimension`` + ``value``, e.g. ``category`` = ``iitr_student``). Each
source reads exactly the rows its report groups (``report_revenue.revenue_rows``) with the same filters and role
scope, so the users of a segment add up to the segment and the segment to the report row it was opened from.

Sources:
- ``performance``: Equipment Performance Report (Main / Department Administrators, OICs, Lab Operators; equipment
  limited by the report view's own scope; money hidden from viewers who may not see report revenue).
- ``finance``: Departmental Finance Report (Accounts In Charge / Main Administrator; the viewer's department).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from django.contrib.auth import get_user_model

from iic_booking.users.user_categories import category_for, category_label

from .report_revenue import (
    DIMENSIONS,
    RevenueRow,
    SegmentContext,
    group,
    money,
    revenue_rows,
    segment_label,
    segment_predicate,
    slot_bounds_annotations,
)

MAX_BOOKINGS = 5000


class DrilldownError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


@dataclass
class DrillSource:
    key: str
    title: str
    date_from: date
    date_to: date
    rows: list[RevenueRow]
    ctx: SegmentContext = field(default_factory=SegmentContext)
    revenue_visible: bool = True
    revenue_definition: str = ""


def _period_display(start: date, end: date) -> str:
    return f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"


def performance_source(date_from, date_to, equipment_ids, include_revenue: bool) -> DrillSource:
    from .report_revenue import report_period, report_revenue_bookings
    from .reports import report_equipment_scope

    start, end = report_period(date_from, date_to)
    equipment_list, equipment_by_id, eq_ids, mode_rollup = report_equipment_scope(equipment_ids)
    rows = revenue_rows(report_revenue_bookings(start, end, eq_ids), mode_rollup) if eq_ids else []
    ctx = SegmentContext(equipment_names={e.equipment_id: e.name or e.code or "" for e in equipment_list})
    for eq in equipment_list:
        for link, attr in ((eq.equipment_managers.all(), "manager"), (eq.equipment_operators.all(), "operator")):
            for row in link:
                person = getattr(row, attr, None)
                if person is None:
                    continue
                ctx.staff_equipment.setdefault(person.pk, set()).add(eq.equipment_id)
                ctx.staff_names.setdefault(person.pk, _display_name(person))
    return DrillSource(
        key="performance",
        title="Equipment Performance Report",
        date_from=start,
        date_to=end,
        rows=rows,
        ctx=ctx,
        revenue_visible=include_revenue,
        revenue_definition="Completed bookings with a slot in the period (test accounts and test equipment left out).",
    )


def finance_source(viewer, date_from: date, date_to: date) -> DrillSource:
    from iic_booking.users.finance_reports import finance_revenue_bookings

    from .models import Equipment

    rows = revenue_rows(finance_revenue_bookings(viewer, date_from, date_to), date_field="effective_date")
    names = dict(
        Equipment.objects.filter(pk__in={r.equipment_id for r in rows}).values_list("equipment_id", "name")
    )
    return DrillSource(
        key="finance",
        title="Departmental Finance Report",
        date_from=date_from,
        date_to=date_to,
        rows=rows,
        ctx=SegmentContext(equipment_names=names),
        revenue_definition="Charged bookings settled, completed or created in the period on your department's equipment.",
    )


def _display_name(user) -> str:
    from iic_booking.users.display import get_user_display_name

    return get_user_display_name(user) or getattr(user, "email", "") or ""


def _segment(source: DrillSource, dimension: str, value: str) -> tuple[list[RevenueRow], dict[str, Any]]:
    dimension = (dimension or "all").strip()
    if dimension not in DIMENSIONS:
        raise DrilldownError(f"Unknown breakdown: {dimension}.")
    if dimension not in ("all", "internal", "external") and not str(value or "").strip():
        raise DrilldownError("Choose a row to drill into.")
    if dimension == "user":
        try:
            uid = int(value)
        except (TypeError, ValueError):
            raise DrilldownError("Choose a row to drill into.") from None
        person = get_user_model().objects.filter(pk=uid).first()
        if person is not None:
            source.ctx.user_names[uid] = _display_name(person)
    keep = segment_predicate(dimension, value, source.ctx)
    rows = [r for r in source.rows if keep(r)]
    total = sum((r.charge for r in rows), Decimal("0"))
    segment = {
        "dimension": dimension,
        "dimension_label": DIMENSIONS[dimension],
        "value": str(value or ""),
        "label": segment_label(dimension, value, source.ctx),
        "bookings": len(rows),
        "users": len({r.user_id for r in rows}),
    }
    if source.revenue_visible:
        segment["revenue"] = money(total)
    return rows, segment


def _header(source: DrillSource) -> dict[str, Any]:
    return {
        "source": source.key,
        "report_title": source.title,
        "date_from": source.date_from.isoformat(),
        "date_to": source.date_to.isoformat(),
        "period_display": _period_display(source.date_from, source.date_to),
        "revenue_definition": source.revenue_definition,
        "revenue_visible": source.revenue_visible,
    }


def _people(user_ids) -> dict[int, Any]:
    return {u.pk: u for u in get_user_model().objects.filter(pk__in=list(user_ids)).select_related("department")}


def _organisation(user) -> str:
    """Department for IITR users; the organisation (an external department) for external users."""
    dept = getattr(user, "department", None)
    return (getattr(dept, "name", "") or "").strip() if dept is not None else ""


def segment_users(source: DrillSource, dimension: str, value: str) -> dict[str, Any]:
    """Users in a segment with their bookings and revenue in it (adds up to the segment)."""
    rows, segment = _segment(source, dimension, value)
    buckets = group(rows, lambda r: r.user_id)
    people = _people(b.key for b in buckets)
    users = []
    for b in buckets:
        person = people.get(b.key)
        code = getattr(person, "user_type", "") if person else ""
        entry = {
            "user_id": b.key,
            "name": _display_name(person) if person else "Deleted user",
            "email": getattr(person, "email", "") or "" if person else "",
            "category": category_for(code),
            "category_label": category_label(code),
            "department": _organisation(person) if person else "",
            "bookings": b.count,
        }
        if source.revenue_visible:
            entry["revenue"] = money(b.total)
        users.append(entry)
    return {**_header(source), "segment": segment, "users": users, "count": len(users)}


def segment_bookings(source: DrillSource, dimension: str, value: str, user_id: Optional[int] = None) -> dict[str, Any]:
    """Bookings in a segment (optionally one user's), newest slot first, with links to each booking."""
    from .models import Booking

    rows, segment = _segment(source, dimension, value)
    user = None
    if user_id is not None:
        rows = [r for r in rows if r.user_id == user_id]
        person = get_user_model().objects.filter(pk=user_id).select_related("department").first()
        if person is None and not rows:
            raise DrilldownError("User not found.", status=404)
        code = getattr(person, "user_type", "") if person else ""
        user = {
            "user_id": user_id,
            "name": _display_name(person) if person else "Deleted user",
            "email": getattr(person, "email", "") or "" if person else "",
            "category_label": category_label(code),
            "department": _organisation(person) if person else "",
            "bookings": len(rows),
        }
        if source.revenue_visible:
            user["revenue"] = money(sum((r.charge for r in rows), Decimal("0")))
    ids = [r.booking_id for r in rows][:MAX_BOOKINGS]
    by_id = {
        b.pk: b
        for b in Booking.objects.filter(pk__in=ids)
        .select_related("equipment", "user", "user__department")
        .annotate(**slot_bounds_annotations())
    }
    bookings = []
    for r in rows[:MAX_BOOKINGS]:
        b = by_id.get(r.booking_id)
        if b is None:
            continue
        entry = {
            "pk": b.pk,
            "display_id": b.virtual_booking_id or str(b.pk),
            "equipment": {"id": b.equipment_id, "name": b.equipment.name or "", "code": b.equipment.code or ""},
            "user": {"id": b.user_id, "name": _display_name(b.user), "email": b.user.email or ""},
            "category_label": category_label(r.snapshot),
            "department": r.department_name,
            "slot_start": b.slot_start.isoformat() if getattr(b, "slot_start", None) else None,
            "slot_end": b.slot_end.isoformat() if getattr(b, "slot_end", None) else None,
            "completed_at": b.completed_at.isoformat() if b.completed_at else None,
            "hours": round(float(b.total_time_minutes or 0) / 60.0, 2),
            "status": b.status,
            "status_display": b.get_status_display(),
        }
        if source.revenue_visible:
            entry["charge"] = money(r.charge)
        bookings.append(entry)
    bookings.sort(key=lambda x: (x["slot_start"] or "", x["pk"]), reverse=True)
    return {
        **_header(source),
        "segment": segment,
        "user": user,
        "bookings": bookings,
        "count": len(bookings),
        "truncated": len(rows) > MAX_BOOKINGS,
    }
