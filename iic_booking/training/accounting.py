"""
Operator duty hours accounting.

Per shift (minutes):
  allocated   – every shift offered, except shifts the OIC cancelled
  confirmed   – shifts of confirmed/completed duty that were not released or cancelled
  operated    – recorded on completed shifts (check-in/out, completed bookings in the window, or the OIC)
  pending     – confirmed shifts that have ended but have no hours recorded yet (awaiting the OIC)
  upcoming    – confirmed shifts that have not started
  missed      – shifts the OIC marked as missed
  released    – declined or unconfirmed (auto-released) shifts
Legacy verified TA duty logs are reported separately as ``legacy_ta_hours``. Honorarium = operated hours × the
rate frozen on the allocation (informational: no payments are made here).
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone
from django.utils.dateparse import parse_date

from iic_booking.users.display import get_user_display_name

from .errors import TrainingError
from .models import DutyAllocation, DutyShift, DutyStatus, ShiftStatus

GROUPS = ("operator", "equipment", "department", "faculty", "month")
CONFIRMED_ALLOC = (DutyStatus.CONFIRMED, DutyStatus.COMPLETED)
METRICS = ("allocated", "confirmed", "operated", "pending", "upcoming", "missed", "released")


def _h(minutes) -> float:
    return round(float(minutes or 0) / 60.0, 2)


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def period(params) -> tuple[datetime, datetime, str]:
    """Resolve ``academic_year`` (YYYY-YY) or ``date_from``/``date_to``; defaults to the current academic year."""
    from iic_booking.equipment import academic_years

    tz = timezone.get_current_timezone()
    d_from = parse_date(str(params.get("date_from") or ""))
    d_to = parse_date(str(params.get("date_to") or ""))
    month = str(params.get("month") or "")
    if month:
        try:
            y, m = (int(x) for x in month.split("-", 1))
            d_from = date(y, m, 1)
            d_to = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
        except (TypeError, ValueError):
            raise TrainingError("month must be YYYY-MM.") from None
        label = d_from.strftime("%B %Y")
    elif d_from or d_to:
        d_from = d_from or date(1970, 1, 1)
        d_to = d_to or timezone.localdate()
        if d_to < d_from:
            raise TrainingError("date_to is before date_from.")
        label = f"{d_from:%d %b %Y} – {d_to:%d %b %Y}"
    else:
        ay = str(params.get("academic_year") or "") or academic_years.current_label()
        try:
            d_from, d_to = academic_years.bounds(ay)
        except academic_years.AcademicYearError as exc:
            raise TrainingError(str(exc)) from None
        label = f"Academic year {ay}"
    return (
        timezone.make_aware(datetime.combine(d_from, time.min), tz),
        timezone.make_aware(datetime.combine(d_to + timedelta(days=1), time.min), tz),
        label,
    )


def _shifts(start: datetime, end: datetime, equipment_ids, params):
    qs = DutyShift.objects.filter(start_at__gte=start, start_at__lt=end).select_related(
        "allocation", "equipment", "operator", "operator__department", "allocation__roster_entry", "allocation__roster_entry__faculty", "verified_by"
    )
    if equipment_ids is not None:
        qs = qs.filter(equipment_id__in=list(equipment_ids))
    for key, field in (("equipment_id", "equipment_id"), ("operator_id", "operator_id")):
        if params.get(key):
            qs = qs.filter(**{field: params.get(key)})
    if params.get("department_id"):
        qs = qs.filter(operator__department_id=params.get("department_id"))
    return qs.order_by("start_at", "id")


def classify(shift: DutyShift, now: datetime) -> dict:
    """Minutes per metric for one shift."""
    planned = shift.planned_minutes
    out = {k: 0 for k in METRICS}
    if shift.status == ShiftStatus.CANCELLED:
        return out
    out["allocated"] = planned
    if shift.status == ShiftStatus.RELEASED:
        out["released"] = planned
        return out
    if shift.status == ShiftStatus.MISSED:
        out["confirmed"] = planned
        out["missed"] = planned
        return out
    if shift.status == ShiftStatus.COMPLETED:
        out["confirmed"] = planned
        out["operated"] = int(shift.operated_minutes or 0)
        return out
    if shift.allocation.status in CONFIRMED_ALLOC:
        out["confirmed"] = planned
        if shift.end_at <= now:
            out["pending"] = planned
        elif shift.start_at > now:
            out["upcoming"] = planned
    return out


def _group_key(shift: DutyShift, group: str) -> tuple[str, str]:
    if group == "equipment":
        return str(shift.equipment_id), shift.equipment.name
    if group == "department":
        dept = shift.operator.department
        return (str(dept.pk), dept.name) if dept else ("", "No department")
    if group == "faculty":
        entry = shift.allocation.roster_entry
        fac = entry.faculty if entry else None
        return (str(fac.pk), get_user_display_name(fac)) if fac else ("", "No faculty group")
    if group == "month":
        m = timezone.localtime(shift.start_at)
        return m.strftime("%Y-%m"), m.strftime("%b %Y")
    return str(shift.operator_id), get_user_display_name(shift.operator)


def _legacy(start: datetime, end: datetime, equipment_ids, params, group: str) -> dict[str, Decimal]:
    """Verified legacy TA duty hours keyed like ``_group_key`` (operator / equipment / department / month)."""
    from iic_booking.equipment.models import TADutyLog, TADutyLogStatus

    qs = TADutyLog.objects.filter(
        status=TADutyLogStatus.VERIFIED,
        duty_date__gte=timezone.localtime(start).date(),
        duty_date__lt=timezone.localtime(end).date(),
    ).select_related("student", "equipment")
    if equipment_ids is not None:
        qs = qs.filter(equipment_id__in=list(equipment_ids))
    if params.get("equipment_id"):
        qs = qs.filter(equipment_id=params.get("equipment_id"))
    if params.get("operator_id"):
        qs = qs.filter(student_id=params.get("operator_id"))
    if params.get("department_id"):
        qs = qs.filter(student__department_id=params.get("department_id"))
    out: dict[str, Decimal] = {}
    for log in qs:
        if group == "equipment":
            key = str(log.equipment_id)
        elif group == "department":
            key = str(log.student.department_id or "")
        elif group == "month":
            key = log.duty_date.strftime("%Y-%m")
        elif group == "faculty":
            key = str(getattr(log.student, "supervisor_id", "") or "")
        else:
            key = str(log.student_id)
        out[key] = out.get(key, Decimal("0")) + (log.hours_spent or Decimal("0"))
    return out


def summary(params, equipment_ids) -> dict:
    """Totals and grouped rows. ``equipment_ids`` None = all equipment (admins)."""
    group = params.get("group_by") or "operator"
    if group not in GROUPS:
        raise TrainingError(f"group_by must be one of {', '.join(GROUPS)}.")
    start, end, label = period(params)
    now = timezone.now()
    groups: dict[str, dict] = {}
    totals = {k: 0 for k in METRICS}
    total_amount = Decimal("0")
    operators: set[int] = set()
    allocations: set[int] = set()
    for shift in _shifts(start, end, equipment_ids, params):
        mins = classify(shift, now)
        amount = (Decimal(mins["operated"]) / Decimal(60)) * (shift.allocation.hourly_rate or Decimal("0"))
        key, name = _group_key(shift, group)
        g = groups.setdefault(key, {"key": key, "label": name, **{k: 0 for k in METRICS}, "amount": Decimal("0"), "shifts": 0, "operators": set()})
        for k in METRICS:
            g[k] += mins[k]
            totals[k] += mins[k]
        g["amount"] += amount
        g["shifts"] += 1
        g["operators"].add(shift.operator_id)
        total_amount += amount
        operators.add(shift.operator_id)
        allocations.add(shift.allocation_id)
    legacy = _legacy(start, end, equipment_ids, params, group)
    rows = []
    for key, g in groups.items():
        rows.append(_row_out(g, legacy.pop(key, Decimal("0"))))
    for key, hours in legacy.items():
        rows.append(_row_out({"key": key, "label": _legacy_label(group, key), **{k: 0 for k in METRICS}, "amount": Decimal("0"), "shifts": 0, "operators": set()}, hours))
    rows.sort(key=lambda r: (-r["operated_hours"], r["label"]))
    if group == "month":
        rows.sort(key=lambda r: r["key"])
    legacy_total = sum((Decimal(str(r["legacy_ta_hours"])) for r in rows), Decimal("0"))
    return {
        "period": {"label": label, "start": start.isoformat(), "end": end.isoformat()},
        "group_by": group,
        "totals": {
            **{f"{k}_hours": _h(v) for k, v in totals.items()},
            "legacy_ta_hours": float(legacy_total),
            "honorarium": _money(total_amount),
            "utilisation_pct": round(totals["operated"] * 100 / totals["confirmed"], 1) if totals["confirmed"] else None,
            "operators": len(operators),
            "allocations": len(allocations),
        },
        "rows": rows,
    }


def _legacy_label(group: str, key: str) -> str:
    if not key:
        return "Unassigned"
    if group == "equipment":
        from iic_booking.equipment.models import Equipment

        e = Equipment.objects.filter(pk=key).first()
        return e.name if e else key
    if group == "department":
        from iic_booking.users.models import Department

        d = Department.objects.filter(pk=key).first()
        return d.name if d else key
    if group == "month":
        return datetime.strptime(key, "%Y-%m").strftime("%b %Y")
    from iic_booking.users.models import User

    u = User.objects.filter(pk=key).first()
    return get_user_display_name(u) if u else key


def _row_out(g: dict, legacy_hours: Decimal) -> dict:
    return {
        "key": g["key"],
        "label": g["label"],
        **{f"{k}_hours": _h(g[k]) for k in METRICS},
        "legacy_ta_hours": float(legacy_hours),
        "honorarium": _money(g["amount"]),
        "utilisation_pct": round(g["operated"] * 100 / g["confirmed"], 1) if g["confirmed"] else None,
        "shifts": g["shifts"],
        "operators": len(g["operators"]),
    }


def shift_line(shift: DutyShift, now: datetime | None = None) -> dict:
    now = now or timezone.now()
    mins = classify(shift, now)
    rate = shift.allocation.hourly_rate or Decimal("0")
    return {
        "shift_id": shift.pk,
        "allocation_id": shift.allocation_id,
        "reference": shift.allocation.reference,
        "date": timezone.localtime(shift.start_at).date().isoformat(),
        "start": shift.start_at.isoformat(),
        "end": shift.end_at.isoformat(),
        "equipment_id": shift.equipment_id,
        "equipment_name": shift.equipment.name,
        "operator_id": shift.operator_id,
        "operator_name": get_user_display_name(shift.operator),
        "status": shift.status,
        "status_label": shift.get_status_display(),
        "allocation_status": shift.allocation.status,
        "planned_hours": _h(shift.planned_minutes),
        "operated_hours": _h(mins["operated"]) if shift.status == ShiftStatus.COMPLETED else None,
        "pending": bool(mins["pending"]),
        "hours_source": shift.hours_source,
        "check_in_at": shift.check_in_at.isoformat() if shift.check_in_at else None,
        "check_out_at": shift.check_out_at.isoformat() if shift.check_out_at else None,
        "verified_by": get_user_display_name(shift.verified_by) if shift.verified_by_id else "",
        "remarks": shift.remarks,
        "hourly_rate": _money(rate),
        "amount": _money((Decimal(mins["operated"]) / Decimal(60)) * rate),
    }


def statement(params, equipment_ids, *, operator_id: int) -> dict:
    """Shift-by-shift statement for one operator (a month by default)."""
    from iic_booking.users.models import User

    params = dict(params.items()) if hasattr(params, "items") else dict(params)
    if not any(params.get(k) for k in ("month", "academic_year", "date_from", "date_to")):
        params["month"] = timezone.localdate().strftime("%Y-%m")
    params["operator_id"] = operator_id
    start, end, label = period(params)
    operator = User.objects.filter(pk=operator_id).first()
    if operator is None:
        raise TrainingError("Operator not found.", status=404)
    now = timezone.now()
    lines = [shift_line(s, now) for s in _shifts(start, end, equipment_ids, params)]
    sums = summary({**params, "group_by": "equipment"}, equipment_ids)
    from iic_booking.equipment.models import TADutyLog, TADutyLogStatus

    legacy_qs = TADutyLog.objects.filter(
        student_id=operator_id,
        status=TADutyLogStatus.VERIFIED,
        duty_date__gte=timezone.localtime(start).date(),
        duty_date__lt=timezone.localtime(end).date(),
    ).select_related("equipment")
    if equipment_ids is not None:
        legacy_qs = legacy_qs.filter(equipment_id__in=list(equipment_ids))
    return {
        "operator": {"id": operator.pk, "name": get_user_display_name(operator), "email": operator.email},
        "period": sums["period"],
        "totals": sums["totals"],
        "by_equipment": sums["rows"],
        "lines": lines,
        "legacy_logs": [
            {
                "id": log.pk,
                "date": log.duty_date.isoformat(),
                "equipment_name": log.equipment.name,
                "hours": float(log.hours_spent or 0),
                "remarks": log.remarks or "",
            }
            for log in legacy_qs.order_by("duty_date")
        ],
    }


def live(equipment_ids) -> dict:
    """Who is on duty now, what is next today, and what needs the OIC's attention."""
    now = timezone.now()
    day_end = timezone.localtime(now).replace(hour=23, minute=59, second=59)
    base = DutyShift.objects.select_related("allocation", "equipment", "operator", "verified_by")
    if equipment_ids is not None:
        base = base.filter(equipment_id__in=list(equipment_ids))
    on_duty = base.filter(
        allocation__status=DutyStatus.CONFIRMED,
        status__in=(ShiftStatus.SCHEDULED, ShiftStatus.CHECKED_IN),
        start_at__lte=now,
        end_at__gt=now,
    ).order_by("start_at")
    later = base.filter(
        allocation__status__in=(DutyStatus.CONFIRMED, DutyStatus.PENDING),
        status=ShiftStatus.SCHEDULED,
        start_at__gt=now,
        start_at__lte=day_end,
    ).order_by("start_at")
    pending_verify = base.filter(
        allocation__status=DutyStatus.CONFIRMED,
        status__in=(ShiftStatus.SCHEDULED, ShiftStatus.CHECKED_IN),
        end_at__lte=now,
    ).order_by("-end_at")
    alloc_qs = DutyAllocation.objects.filter(status=DutyStatus.PENDING)
    if equipment_ids is not None:
        alloc_qs = alloc_qs.filter(equipment_id__in=list(equipment_ids))
    return {
        "now": now.isoformat(),
        "on_duty": [{**shift_line(s, now), "checked_in": s.status == ShiftStatus.CHECKED_IN} for s in on_duty[:100]],
        "later_today": [shift_line(s, now) for s in later[:100]],
        "pending_verification": [shift_line(s, now) for s in pending_verify[:100]],
        "pending_verification_count": pending_verify.count(),
        "awaiting_confirmation_count": alloc_qs.count(),
        "due_within_24h_count": alloc_qs.filter(confirm_by__lte=now + timedelta(hours=24)).count(),
    }


def export_csv(params, equipment_ids) -> str:
    start, end, _ = period(params)
    now = timezone.now()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "Reference", "Date", "Start", "End", "Equipment", "Operator", "Shift status", "Duty status",
            "Planned hours", "Operated hours", "Hours source", "Verified by", "Rate", "Amount", "Remarks",
        ]
    )
    for s in _shifts(start, end, equipment_ids, params):
        line = shift_line(s, now)
        w.writerow(
            [
                line["reference"], line["date"],
                timezone.localtime(s.start_at).strftime("%H:%M"), timezone.localtime(s.end_at).strftime("%H:%M"),
                line["equipment_name"], line["operator_name"], line["status_label"], s.allocation.get_status_display(),
                line["planned_hours"], "" if line["operated_hours"] is None else line["operated_hours"],
                s.get_hours_source_display() if s.hours_source else "", line["verified_by"],
                line["hourly_rate"], line["amount"], (line["remarks"] or "").replace("\n", " "),
            ]
        )
    return buf.getvalue()
