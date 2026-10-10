"""Cancellations dashboard and the dashboard's Cancellations card.

A cancellation is a booking that ended without being carried out — cancelled, refunded, or stopped by the lab
(under maintenance, operator unavailable, analysis not possible) — counted once, on the day it happened
(``BookingCancellation.cancelled_at``). Bookings marked not utilized (no-shows) are tracked too but only included when
asked for. Bookings of test accounts are excluded; a Department Administrator sees their department's equipment.

- Rate: cancellations ÷ bookings created in the same dates (same equipment / user filters).
- Late: less than 24 hours before the first booked slot started (or after it started).
- Refund: the amount credited back when recorded; otherwise the full charge for refunded / lab-stopped bookings
  (marked estimated). Charges retained = charge − refund over cancellations whose refund is known.
- Re-booked: a released slot is now held by a booking made after the cancellation (from the waitlist when that
  booking was created by a waitlist allocation); unknown when the released slots were not recorded.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import BooleanField, Case, CharField, Count, F, Q, Sum, Value, When
from django.db.models.functions import TruncDate, TruncWeek
from django.utils import timezone

from iic_booking.equipment.admin_dashboard_summary import _Scope

from .common import bounds, flag, int_values, iso, money, multi, page_meta, page_params, period, scope_payload, share
from .users import CATEGORIES, category_case

TOP = 15
LATE_MINUTES = 24 * 60
LATE = Q(lead_minutes__lt=LATE_MINUTES)
CARD_DAYS = 30
LEAD_BUCKETS = (
    ("after_start", "After the slot started", None, 0),
    ("under_2h", "Less than 2 hours before", 0, 120),
    ("2_24h", "2–24 hours before", 120, LATE_MINUTES),
    ("1_3d", "1–3 days before", LATE_MINUTES, 3 * LATE_MINUTES),
    ("3_7d", "3–7 days before", 3 * LATE_MINUTES, 7 * LATE_MINUTES),
    ("over_7d", "More than 7 days before", 7 * LATE_MINUTES, None),
)
REFILL_LABELS = {
    "waitlist": "Re-booked from the waitlist",
    "rebooked": "Re-booked by another booking",
    "not_refilled": "Not re-booked",
    "unknown": "Not known",
}
SORTS = {
    "cancelled_at": "cancelled_at",
    "lead": "lead_minutes",
    "refund": "refund_amount",
    "charge": "charge_amount",
    "slot": "slot_start",
}


def _choices():
    from iic_booking.equipment.models import CancellationActorRole, CancellationDataQuality, CancellationReason

    return (
        {k: str(v) for k, v in CancellationActorRole.choices},
        {k: str(v) for k, v in CancellationReason.choices},
        {k: str(v) for k, v in CancellationDataQuality.choices},
    )


def _scoped(user):
    """Cancellation rows of bookings on the user's Reports scope (test accounts excluded)."""
    from iic_booking.equipment.booking_cancellation_log import TRACKED_STATUSES
    from iic_booking.equipment.booking_report_metrics import report_bookings_scope
    from iic_booking.equipment.models import BookingCancellation

    bookings, _ = report_bookings_scope(user)
    case, code = category_case("booking__user__")
    lead_bucket = Case(
        *[
            When(
                Q(**({"lead_minutes__gte": low} if low is not None else {}))
                & Q(**({"lead_minutes__lt": high} if high is not None else {}))
                & Q(lead_minutes__isnull=False),
                then=Value(key),
            )
            for key, _label, low, high in LEAD_BUCKETS
        ],
        default=Value("unknown"),
        output_field=CharField(),
    )
    return (
        BookingCancellation.objects.filter(
            booking_id__in=bookings.order_by().values("pk"), booking__status__in=sorted(TRACKED_STATUSES)
        )
        .order_by()
        .annotate(user_type_code=code)
        .annotate(
            category=case,
            is_late=Case(When(LATE, then=Value(True)), default=Value(False), output_field=BooleanField()),
            lead_bucket=lead_bucket,
        )
    )


def _apply_filters(qs, params, *, booking_prefix: str = "booking__"):
    """Filters shared by cancellations and the bookings used for the rate (``booking_prefix`` = path to Booking)."""
    p = booking_prefix
    equipment = int_values(multi(params, "equipment"))
    if equipment:
        qs = qs.filter(**{f"{p}equipment_id__in": equipment})
    departments = multi(params, "department")
    if departments:
        q = Q(**{f"{p}user__department_id__in": int_values(departments)})
        if "none" in departments:
            q |= Q(**{f"{p}user__department__isnull": True})
        qs = qs.filter(q)
    categories = [c for c in multi(params, "category") if c in CATEGORIES]
    if categories:
        qs = qs.filter(category__in=categories)
    oics = int_values(multi(params, "oic"))
    if oics:
        from iic_booking.equipment.models import EquipmentManager

        managed = EquipmentManager.objects.filter(manager_id__in=oics).values("equipment_id")
        qs = qs.filter(**{f"{p}equipment_id__in": managed})
    return qs


def _filtered(qs, params, start, end):
    start_at, end_at = bounds(start, end)
    qs = qs.filter(cancelled_at__gte=start_at, cancelled_at__lt=end_at)
    if not flag(params, "include_no_shows"):
        qs = qs.exclude(reason="NO_SHOW")
    qs = _apply_filters(qs, params)
    roles = multi(params, "role")
    if roles:
        qs = qs.filter(actor_role__in=[r.upper() for r in roles])
    reasons = multi(params, "reason")
    if reasons:
        qs = qs.filter(reason__in=[r.upper() for r in reasons])
    qualities = multi(params, "data_quality")
    if qualities:
        qs = qs.filter(data_quality__in=[q.upper() for q in qualities])
    if flag(params, "late_only"):
        qs = qs.filter(LATE)
    search = str(params.get("search") or "").strip()
    if search:
        q = (
            Q(booking__virtual_booking_id__icontains=search)
            | Q(booking__user__name__icontains=search)
            | Q(booking__user__email__icontains=search)
            | Q(booking__equipment__name__icontains=search)
            | Q(booking__equipment__code__icontains=search)
            | Q(note__icontains=search)
        )
        if search.isdigit():
            q |= Q(booking_id=int(search))
        qs = qs.filter(q)
    return qs


def _bookings_created(user, params, start, end) -> int:
    from iic_booking.equipment.booking_report_metrics import report_bookings_scope

    bookings, _ = report_bookings_scope(user)
    start_at, end_at = bounds(start, end)
    case, code = category_case("user__")
    qs = bookings.order_by().filter(created_at__gte=start_at, created_at__lt=end_at)
    if multi(params, "category"):
        qs = qs.annotate(user_type_code=code).annotate(category=case)
    return _apply_filters(qs, params, booking_prefix="").count()


def _totals(qs) -> dict[str, Any]:
    known = Q(refund_amount__isnull=False)
    row = qs.aggregate(
        total=Count("pk"),
        late=Count("pk", filter=LATE),
        charge=Sum("charge_amount"),
        refund=Sum("refund_amount"),
        charge_known=Sum("charge_amount", filter=known),
        refund_unknown=Count("pk", filter=Q(refund_amount__isnull=True)),
        refund_estimated=Count("pk", filter=Q(refund_estimated=True)),
        refunded=Count("pk", filter=Q(refund_amount__gt=0)),
    )
    retained = Decimal(row["charge_known"] or 0) - Decimal(row["refund"] or 0)
    return {
        "total": row["total"] or 0,
        "late": row["late"] or 0,
        "late_share": share(row["late"] or 0, row["total"] or 0),
        "charge_total": money(row["charge"]),
        "refund_total": money(row["refund"]),
        "retained_total": money(max(retained, Decimal("0"))),
        "refunded_count": row["refunded"] or 0,
        "refund_unknown": row["refund_unknown"] or 0,
        "refund_estimated": row["refund_estimated"] or 0,
    }


def _group(qs, field: str, labels: dict | None = None, *, top: int | None = None, extra=None) -> list[dict]:
    rows = (
        qs.values(field)
        .annotate(
            count=Count("pk"),
            late=Count("pk", filter=LATE),
            refund=Sum("refund_amount"),
            **(extra or {}),
        )
        .order_by("-count")
    )
    if top:
        rows = rows[:top]
    out = []
    for r in rows:
        key = r[field]
        item = {
            "key": key if key not in (None, "") else "none",
            "label": (labels or {}).get(key, key) if key not in (None, "") else "Not set",
            "count": r["count"],
            "late": r["late"],
            "refund": money(r["refund"]),
        }
        for name in extra or {}:
            item[name] = r[name]
        out.append(item)
    return out


def _named_group(qs, id_field: str, name_field: str, *, none_label: str, extra_fields=()) -> list[dict]:
    rows = (
        qs.values(id_field, name_field, *extra_fields)
        .annotate(count=Count("pk", distinct=True), late=Count("pk", filter=LATE, distinct=True))
        .order_by("-count", name_field)[:TOP]
    )
    out = []
    for r in rows:
        item = {
            "id": r[id_field],
            "label": r[name_field] or none_label,
            "count": r["count"],
            "late": r["late"],
        }
        for name in extra_fields:
            item[name.split("__")[-1]] = r[name]
        out.append(item)
    return out


def _trend(qs, start, end) -> dict[str, Any]:
    days = (end - start).days + 1
    weekly = days > 62
    tz = timezone.get_current_timezone()
    trunc = TruncWeek("cancelled_at", tzinfo=tz) if weekly else TruncDate("cancelled_at", tzinfo=tz)
    rows = qs.annotate(bucket=trunc).values("bucket").annotate(n=Count("pk"), late=Count("pk", filter=LATE))
    found = {}
    for r in rows:
        bucket = r["bucket"]
        day = timezone.localtime(bucket).date() if hasattr(bucket, "hour") else bucket
        found[day] = r
    series = []
    cursor = start - timedelta(days=start.weekday()) if weekly else start
    step = timedelta(weeks=1) if weekly else timedelta(days=1)
    while cursor <= end:
        r = found.get(cursor) or {}
        series.append({"period": cursor.isoformat(), "count": r.get("n") or 0, "late": r.get("late") or 0})
        cursor += step
    return {"granularity": "week" if weekly else "day", "series": series}


def refill_status(rows: list[dict]) -> dict[int, str]:
    """Cancellation id -> ``waitlist`` / ``rebooked`` / ``not_refilled`` / ``unknown`` (see module docstring)."""
    from iic_booking.equipment.models import BookingEvent, BookingEventType, DailySlot

    slot_ids = {int(s) for r in rows for s in (r["released_slot_ids"] or []) if str(s).isdigit()}
    holders: dict[int, tuple[int, Any]] = {}
    ids = sorted(slot_ids)
    for i in range(0, len(ids), 5000):
        for s in DailySlot.objects.filter(
            pk__in=ids[i : i + 5000], booking__isnull=False, booking__user__is_test_account=False
        ).values("pk", "booking_id", "booking__created_at"):
            holders[s["pk"]] = (s["booking_id"], s["booking__created_at"])
    holder_bookings = sorted({b for b, _ in holders.values()})
    from_waitlist = set()
    for i in range(0, len(holder_bookings), 5000):
        from_waitlist |= set(
            BookingEvent.objects.filter(
                booking_id__in=holder_bookings[i : i + 5000],
                event_type=BookingEventType.CREATED,
                metadata__from_waitlist=True,
            ).values_list("booking_id", flat=True)
        )
    out = {}
    for r in rows:
        released = [int(s) for s in (r["released_slot_ids"] or []) if str(s).isdigit()]
        if not released:
            out[r["id"]] = "unknown"
            continue
        refilled = {
            holders[s][0]
            for s in released
            if s in holders and holders[s][0] != r["booking_id"] and holders[s][1] and holders[s][1] > r["cancelled_at"]
        }
        if refilled & from_waitlist:
            out[r["id"]] = "waitlist"
        elif refilled:
            out[r["id"]] = "rebooked"
        else:
            out[r["id"]] = "not_refilled"
    return out


def _options(user, scope: _Scope) -> dict[str, Any]:
    from iic_booking.equipment.models import Equipment, EquipmentManager
    from iic_booking.users.display import get_user_display_name

    roles, reasons, qualities = _choices()
    equipment = scope.by_department(Equipment.objects.all(), "internal_department_id").order_by("name")
    managers = {}
    for em in EquipmentManager.objects.filter(equipment_id__in=equipment.values("pk")).select_related("manager"):
        managers[em.manager_id] = get_user_display_name(em.manager)
    departments = (
        _scoped(user)
        .exclude(booking__user__department__isnull=True)
        .values("booking__user__department_id", "booking__user__department__name")
        .distinct()
        .order_by("booking__user__department__name")
    )
    return {
        "equipment": [{"id": e["equipment_id"], "name": e["name"], "code": e["code"]} for e in equipment.values(
            "equipment_id", "name", "code"
        )],
        "roles": [{"value": k, "label": v} for k, v in roles.items()],
        "reasons": [{"value": k, "label": v} for k, v in reasons.items()],
        "data_qualities": [{"value": k, "label": v} for k, v in qualities.items()],
        "categories": [{"value": k, "label": v} for k, v in CATEGORIES.items()],
        "departments": [
            {"id": d["booking__user__department_id"], "name": d["booking__user__department__name"]}
            for d in departments
        ],
        "oics": [{"id": k, "name": v} for k, v in sorted(managers.items(), key=lambda kv: kv[1].lower())],
    }


def _row(c, roles, reasons, qualities, refills) -> dict[str, Any]:
    from iic_booking.users.display import get_user_display_name

    booking = c.booking
    owner = booking.user
    equipment = booking.equipment
    return {
        "id": c.pk,
        "booking": {
            "pk": booking.pk,
            "display_id": booking.virtual_booking_id or str(booking.pk),
            "status": booking.status,
            "status_display": booking.get_status_display(),
        },
        "user": {
            "id": owner.pk,
            "name": get_user_display_name(owner),
            "category": c.category,
            "category_display": CATEGORIES.get(c.category, "Other"),
            "department": getattr(owner.department, "name", "") if owner.department_id else "",
        },
        "equipment": {"id": equipment.pk, "name": equipment.name, "code": equipment.code},
        "slot_start": iso(c.slot_start),
        "slot_end": iso(c.slot_end),
        "cancelled_at": iso(c.cancelled_at),
        "actor_role": c.actor_role,
        "actor_role_display": roles.get(c.actor_role, c.actor_role),
        "cancelled_by": get_user_display_name(c.cancelled_by) if c.cancelled_by_id else "",
        "reason": c.reason,
        "reason_display": reasons.get(c.reason, c.reason),
        "note": c.note,
        "lead_minutes": c.lead_minutes,
        "late": bool(c.is_late),
        "charge": money(c.charge_amount),
        "refund": money(c.refund_amount) if c.refund_amount is not None else None,
        "refund_estimated": c.refund_estimated,
        "data_quality": c.data_quality,
        "data_quality_display": qualities.get(c.data_quality, c.data_quality),
        "refill": refills.get(c.pk, "unknown"),
        "refill_display": REFILL_LABELS[refills.get(c.pk, "unknown")],
    }


def build_cancellation_insights(user, params) -> dict[str, Any]:
    scope = _Scope(user)
    now = timezone.now()
    start, end = period(params, now=now)
    roles, reasons, qualities = _choices()
    base = _scoped(user)
    qs = _filtered(base, params, start, end)

    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    previous = _totals(_filtered(base, params, prev_start, prev_end))
    totals = _totals(qs)
    created = _bookings_created(user, params, start, end)
    created_prev = _bookings_created(user, params, prev_start, prev_end)

    refill_rows = list(qs.values("id", "booking_id", "cancelled_at", "released_slot_ids"))
    refills = refill_status(refill_rows)
    refill_counts = defaultdict(int)
    for value in refills.values():
        refill_counts[value] += 1

    lead_labels = {key: label for key, label, _low, _high in LEAD_BUCKETS}
    by_lead = {r["key"]: r for r in _group(qs, "lead_bucket", {**lead_labels, "unknown": "Not known"})}
    summary = {
        **totals,
        "bookings_created": created,
        "rate": share(totals["total"], created),
        "previous": {
            "date_from": prev_start.isoformat(),
            "date_to": prev_end.isoformat(),
            "total": previous["total"],
            "late": previous["late"],
            "bookings_created": created_prev,
            "rate": share(previous["total"], created_prev),
        },
        "change": totals["total"] - previous["total"],
        "by_role": _group(qs, "actor_role", roles),
        "by_reason": _group(qs, "reason", reasons),
        "by_lead_time": [
            by_lead.get(key) or {"key": key, "label": label, "count": 0, "late": 0, "refund": 0.0}
            for key, label in [*lead_labels.items(), ("unknown", "Not known")]
        ],
        "by_category": _group(qs, "category", CATEGORIES),
        "by_data_quality": _group(qs, "data_quality", qualities),
        "by_equipment": _named_group(
            qs,
            "booking__equipment_id",
            "booking__equipment__name",
            none_label="Equipment",
            extra_fields=("booking__equipment__code",),
        ),
        "by_department": _named_group(
            qs, "booking__user__department_id", "booking__user__department__name", none_label="No department"
        ),
        "by_oic": _named_group(
            qs,
            "booking__equipment__equipment_managers__manager_id",
            "booking__equipment__equipment_managers__manager__name",
            none_label="No OIC assigned",
        ),
        "refills": [
            {"key": key, "label": label, "count": refill_counts.get(key, 0)} for key, label in REFILL_LABELS.items()
        ],
        "trend": _trend(qs, start, end),
    }

    sort = str(params.get("sort") or "-cancelled_at").strip()
    column = F(SORTS.get(sort.lstrip("-"), "cancelled_at"))
    ordered = qs.select_related(
        "booking", "booking__user", "booking__user__department", "booking__equipment", "cancelled_by"
    ).order_by(column.desc(nulls_last=True) if sort.startswith("-") else column.asc(nulls_last=True), "-pk")
    offset, size = page_params(params)
    page = list(ordered[offset : offset + size])
    payload = {
        **scope_payload(scope),
        "generated_at": now.isoformat(),
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "include_no_shows": flag(params, "include_no_shows"),
        "late_minutes": LATE_MINUTES,
        "summary": summary,
        "results": [_row(c, roles, reasons, qualities, refills) for c in page],
        **page_meta(totals["total"], offset, size),
    }
    if params.get("with_options"):
        payload["options"] = _options(user, scope)
    return payload


def cancellation_card(user, now) -> dict[str, Any]:
    """Dashboard card: cancellations in the last ``CARD_DAYS`` days against the ``CARD_DAYS`` before."""
    today = timezone.localdate(now)
    start = today - timedelta(days=CARD_DAYS - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=CARD_DAYS - 1)
    base = _scoped(user).exclude(reason="NO_SHOW")
    current = base.filter(cancelled_at__gte=bounds(start, today)[0], cancelled_at__lt=bounds(start, today)[1])
    row = current.aggregate(total=Count("pk"), late=Count("pk", filter=LATE), refund=Sum("refund_amount"))
    p0, p1 = bounds(prev_start, prev_end)
    previous_total = base.filter(cancelled_at__gte=p0, cancelled_at__lt=p1).count()
    created = _bookings_created(user, {}, start, today)
    total = row["total"] or 0
    return {
        "days": CARD_DAYS,
        "total": total,
        "previous_total": previous_total,
        "late": row["late"] or 0,
        "refunded": money(row["refund"]),
        "bookings_created": created,
        "rate": share(total, created),
    }
