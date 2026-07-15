"""
Equipment utilization, performance metrics, and OIC report data.

Computes per-equipment stats (users, samples, hours, availability windows, ratings)
for a date range. Used by admin report API, PDF/Excel exports, and monthly emails.
"""

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Optional

from django.db.models import Count, Prefetch, Q, Sum
from django.utils import timezone

from iic_booking.users.models.user_type import UserType

from .models import (
    Booking,
    BookingStatus,
    DailySlot,
    Equipment,
    EquipmentManager,
    EquipmentOperator,
    Holiday,
    SlotStatus,
)


def _parse_sample_count_from_input_values(input_values: dict | None) -> int:
    """Sample count from dynamic input field 'A' (number or numeric string)."""
    if not input_values or not isinstance(input_values, dict):
        return 0
    raw = input_values.get("A")
    if raw is None or raw is False:
        return 0
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        try:
            return max(0, int(raw))
        except (ValueError, TypeError):
            return 0
    s = str(raw).strip()
    if not s:
        return 0
    try:
        return max(0, int(float(s)))
    except (ValueError, TypeError):
        return 0


def _slot_row_hours(start_dt, end_dt) -> float:
    if not start_dt or not end_dt:
        return 0.0
    delta = end_dt - start_dt
    return max(0.0, delta.total_seconds() / 3600.0)


def _normalize_user_type_snapshot(ut: str | None) -> str:
    return (ut or "").strip().lower()


def _is_internal_snapshot(ut: str | None) -> bool:
    n = _normalize_user_type_snapshot(ut)
    return n in {
        UserType.STUDENT,
        UserType.INDIVIDUAL_STUDENT,
        UserType.FACULTY,
    }


def _is_external_snapshot(ut: str | None) -> bool:
    return UserType.is_external_user(ut or "")


def _slot_in_weekly_time_window(eq: Equipment, start_dt, end_dt) -> bool:
    """
    True if slot lies within equipment weekly_view_time_from / weekly_view_time_to (inclusive).
    Empty from/to means no limit.
    """
    w_from = getattr(eq, "weekly_view_time_from", None)
    w_to = getattr(eq, "weekly_view_time_to", None)
    if not w_from and not w_to:
        return True
    if not start_dt or not end_dt:
        return True
    t_start = start_dt.time()
    t_end = end_dt.time()
    if w_from and t_start < w_from:
        return False
    if w_to and t_end > w_to:
        return False
    return True


def _institute_holiday_dates_in_range(start: date, end: date) -> set[date]:
    return set(
        Holiday.objects.filter(
            date__gte=start,
            date__lte=end,
            is_active=True,
        ).values_list("date", flat=True)
    )


def _is_normal_working_calendar_day(d: date, institute_holidays: set[date]) -> bool:
    """Mon–Fri, excluding institute holiday dates (Sat/Sun excluded by weekday)."""
    if d.weekday() >= 5:
        return False
    return d not in institute_holidays


def _is_weekend_or_institute_holiday_day(d: date, institute_holidays: set[date]) -> bool:
    return d.weekday() >= 5 or d in institute_holidays


# Bookings counted as "served" for user/sample/hour stats (had slots in period).
_SERVED_EXCLUDE_STATUSES = frozenset(
    {
        BookingStatus.CANCELLED,
        BookingStatus.REFUNDED,
        BookingStatus.WAITLISTED,
        BookingStatus.PENDING,
    }
)

_RATING_FIELDS = [
    ("rating_on_time_operator_availability", "on_time_operator_availability"),
    ("rating_laboratory_cleanliness_organization", "laboratory_cleanliness_organization"),
    ("rating_sample_handling_care", "sample_handling_care"),
    ("rating_operator_behaviour_professionalism", "operator_behaviour_professionalism"),
    ("rating_compliance_booking_request_parameters", "compliance_booking_request_parameters"),
]


def _aggregate_ratings_for_equipment(
    equipment_id: int,
    start: date,
    end: date,
) -> dict[str, Any]:
    from iic_booking.users.test_accounts import exclude_test_bookings

    qs = exclude_test_bookings(
        Booking.objects.filter(
            equipment_id=equipment_id,
            rated_at__isnull=False,
            rating_removed=False,
            rated_at__date__gte=start,
            rated_at__date__lte=end,
        )
    )
    n = qs.count()
    out: dict[str, Any] = {
        "ratings_submitted_count": n,
        "criteria": {},
    }
    if n == 0:
        return out
    for model_field, key in _RATING_FIELDS:
        yes = qs.filter(**{model_field: True}).count()
        no = qs.filter(**{model_field: False}).count()
        out["criteria"][key] = {
            "yes": yes,
            "no": no,
            "unanswered": n - yes - no,
        }
    ratings_only = [x for x in qs.values_list("rating", flat=True) if x is not None]
    out["overall_rating_avg"] = round(sum(ratings_only) / len(ratings_only), 2) if ratings_only else None
    out["overall_rating_min"] = min(ratings_only) if ratings_only else None
    out["overall_rating_max"] = max(ratings_only) if ratings_only else None
    feedback_list = [
        str(t).strip()
        for t in qs.exclude(rating_feedback__isnull=True).exclude(rating_feedback="").values_list("rating_feedback", flat=True)[:50]
    ]
    out["sample_feedback_texts"] = feedback_list
    return out


def get_lab_operator_emails_for_equipment(equipment_id: int) -> list[str]:
    """Lab Incharge (operator) emails for equipment."""
    rows = EquipmentOperator.objects.filter(equipment_id=equipment_id).select_related("operator")
    return [r.operator.email for r in rows if r.operator and getattr(r.operator, "email", None)]


def get_oic_emails_for_equipment(equipment_id: int) -> list[str]:
    """Return list of OIC (manager) emails for the given equipment."""
    managers = EquipmentManager.objects.filter(equipment_id=equipment_id).select_related("manager")
    return [m.manager.email for m in managers if m.manager and getattr(m.manager, "email", None)]


def get_equipment_staff_notify_users(equipment) -> list:
    """
    Active Officer In Charge (managers), temporary OIC, and Lab Incharge (operators)
    assigned to this equipment — for booking event email + in-app notifications.
    Deduplicated by user id. Prefers active accounts; includes users without email
    so push/in-app can still be delivered when possible.
    """
    if equipment is None:
        return []
    eid = getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)
    if eid is None:
        return []

    from iic_booking.equipment.models import EquipmentTemporaryOIC

    seen: set[int] = set()
    out: list = []

    def _add(u) -> None:
        if not u or not getattr(u, "id", None):
            return
        if u.id in seen:
            return
        if getattr(u, "is_active", True) is False:
            return
        seen.add(u.id)
        out.append(u)

    for em in EquipmentManager.objects.filter(equipment_id=eid).select_related("manager"):
        _add(em.manager)
    now = timezone.now()
    for row in EquipmentTemporaryOIC.objects.filter(
        equipment_id=eid, resume_at__gt=now
    ).select_related("temporary_oic"):
        _add(row.temporary_oic)
    for eo in EquipmentOperator.objects.filter(equipment_id=eid).select_related("operator"):
        _add(eo.operator)
    return out


def get_equipment_ids_managed_by_oic(user_id: int) -> list[int]:
    """Return equipment IDs for which the user is OIC (manager) or temporary OIC (until resume_at)."""
    from iic_booking.equipment.models import EquipmentTemporaryOIC

    primary_ids = set(
        EquipmentManager.objects.filter(manager_id=user_id).values_list("equipment_id", flat=True)
    )
    now = timezone.now()
    temp_ids = set(
        EquipmentTemporaryOIC.objects.filter(
            temporary_oic_id=user_id,
            resume_at__gt=now,
        ).values_list("equipment_id", flat=True)
    )
    return list(primary_ids | temp_ids)


def get_equipment_report_data(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    equipment_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    """
    Compute per-equipment performance stats and overall utilization for the given date range.
    """
    today = timezone.localdate()
    if date_from:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").date()
        except ValueError:
            start = today.replace(day=1)
    else:
        start = today.replace(day=1)
    if date_to:
        try:
            end = datetime.strptime(date_to, "%Y-%m-%d").date()
        except ValueError:
            end = today
    else:
        end = start.replace(day=monthrange(start.year, start.month)[1])

    institute_holidays = _institute_holiday_dates_in_range(start, end)

    qs_equipment = Equipment.objects.all().order_by("code")
    if equipment_ids is not None:
        qs_equipment = qs_equipment.filter(equipment_id__in=equipment_ids)

    equipment_list = list(
        qs_equipment.prefetch_related(
            Prefetch(
                "equipment_managers",
                queryset=EquipmentManager.objects.select_related("manager"),
            ),
            Prefetch(
                "equipment_operators",
                queryset=EquipmentOperator.objects.select_related("operator"),
            ),
        )
    )
    eq_ids = [e.equipment_id for e in equipment_list]
    def _report_title_for_equipment(base: str, eqs: list) -> str:
        if not eqs:
            return base
        if len(eqs) == 1:
            e = eqs[0]
            name = (getattr(e, "name", None) or "").strip()
            if name:
                return f"{base} for {name}"
            code = (getattr(e, "code", None) or "").strip()
            return f"{base} for {code}" if code else base
        return f"{base} for {len(eqs)} instruments"

    if not eq_ids:
        _today = timezone.localdate()
        _human = f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"
        _dur_suffix = " (till current date)" if end == _today else ""
        return {
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "report_header": {
                "institute_name": "Institute Instrumentation Centre",
                "organization": "Indian Institute of Technology Roorkee",
                "report_title": "Equipment Performance Report",
                "period_display": _human,
                "month_year": _human,
                "report_duration_suffix": _dur_suffix,
            },
            "equipment": [],
            "utilization_pie": [{"name": "No data", "value": 0, "hours": 0}],
            "summary": {
                "total_equipment": 0,
                "total_hours": 0.0,
                "utilized_hours": 0.0,
                "downtime_hours": 0.0,
                "utilization_factor": 0.0,
                "revenue_total": 0.0,
                "revenue_internal": 0.0,
                "revenue_external": 0.0,
                "available_hours_working_window": 0.0,
                "completed_hours_in_working_window": 0.0,
                "utilization_vs_working_capacity": 0.0,
            },
            "financial": {
                "revenue_by_user_type": [],
                "revenue_by_department": [],
                "revenue_by_user": [],
                "revenue_by_equipment": [],
                "revenue_by_external_category": [],
            },
        }

    slots_in_range = (
        DailySlot.objects.filter(
            date__gte=start,
            date__lte=end,
            slot_master__equipment_id__in=eq_ids,
        )
        .select_related("slot_master", "slot_master__equipment", "booking", "booking__user")
    )

    eq_slot_stats: dict[int, dict[str, float | int]] = {}
    eq_perf_slots: dict[int, dict[str, float]] = defaultdict(
        lambda: {
            "available_hours_working_window": 0.0,
            "available_hours_weekend_or_holiday": 0.0,
            "completed_hours_working_window": 0.0,
            "blocked_hours": 0.0,
            "other_disruption_hours": 0.0,
        }
    )

    for eid in eq_ids:
        eq_slot_stats[eid] = {
            "under_maintenance_slots": 0,
            "under_maintenance_hours": 0.0,
            "operator_absent_slots": 0,
            "operator_absent_hours": 0.0,
            "booking_not_utilized_slots": 0,
            "booking_not_utilized_hours": 0.0,
            "no_booking_slots": 0,
            "no_booking_hours": 0.0,
            "booked_slots": 0,
            "booked_hours": 0.0,
        }

    for ds in slots_in_range.iterator(chunk_size=500):
        eid = ds.slot_master.equipment_id
        if eid not in eq_slot_stats:
            continue
        eq = ds.slot_master.equipment
        hrs = _slot_row_hours(ds.start_datetime, ds.end_datetime)
        st = ds.status
        bk = ds.booking
        # Test-account bookings must not affect utilization / disruption stats.
        is_test_bk = bool(
            bk is not None and getattr(getattr(bk, "user", None), "is_test_account", False)
        )

        if st == SlotStatus.UNDER_MAINTENANCE:
            eq_slot_stats[eid]["under_maintenance_slots"] += 1
            eq_slot_stats[eid]["under_maintenance_hours"] += hrs
        elif st == SlotStatus.OPERATOR_ABSENT:
            eq_slot_stats[eid]["operator_absent_slots"] += 1
            eq_slot_stats[eid]["operator_absent_hours"] += hrs
        elif st == SlotStatus.BOOKING_NOT_UTILIZED and not is_test_bk:
            eq_slot_stats[eid]["booking_not_utilized_slots"] += 1
            eq_slot_stats[eid]["booking_not_utilized_hours"] += hrs
        elif st == SlotStatus.AVAILABLE:
            eq_slot_stats[eid]["no_booking_slots"] += 1
            eq_slot_stats[eid]["no_booking_hours"] += hrs
        elif st == SlotStatus.BOOKED and not is_test_bk:
            eq_slot_stats[eid]["booked_slots"] += 1
            eq_slot_stats[eid]["booked_hours"] += hrs
        elif st == SlotStatus.BLOCKED:
            eq_perf_slots[eid]["blocked_hours"] += hrs
        elif st == SlotStatus.NOT_AVAILABLE:
            eq_perf_slots[eid]["blocked_hours"] += hrs

        if bk and not is_test_bk and bk.status == BookingStatus.OTHER_DISRUPTION:
            eq_perf_slots[eid]["other_disruption_hours"] += hrs

        d = ds.date
        in_window = _slot_in_weekly_time_window(eq, ds.start_datetime, ds.end_datetime)
        if in_window and _is_normal_working_calendar_day(d, institute_holidays):
            eq_perf_slots[eid]["available_hours_working_window"] += hrs
            if bk and not is_test_bk and bk.status == BookingStatus.COMPLETED:
                eq_perf_slots[eid]["completed_hours_working_window"] += hrs
        if in_window and _is_weekend_or_institute_holiday_day(d, institute_holidays):
            eq_perf_slots[eid]["available_hours_weekend_or_holiday"] += hrs

    from iic_booking.users.test_accounts import exclude_test_bookings

    bookings_in_range = exclude_test_bookings(
        Booking.objects.filter(
            equipment_id__in=eq_ids,
            daily_slots__date__gte=start,
            daily_slots__date__lte=end,
        ).distinct()
    )
    bookings_served = bookings_in_range.exclude(status__in=_SERVED_EXCLUDE_STATUSES)

    completed_in_range = bookings_in_range.filter(status=BookingStatus.COMPLETED)
    overall_bookings_qs = exclude_test_bookings(Booking.objects.filter(equipment_id__in=eq_ids))
    overall_current = overall_bookings_qs.filter(status=BookingStatus.BOOKED)

    in_range_count = dict(
        bookings_in_range.values("equipment_id").annotate(c=Count("booking_id")).values_list("equipment_id", "c")
    )
    completed_in_range_count = dict(
        completed_in_range.values("equipment_id").annotate(c=Count("booking_id")).values_list("equipment_id", "c")
    )
    overall_count = dict(
        overall_bookings_qs.values("equipment_id").annotate(c=Count("booking_id")).values_list("equipment_id", "c")
    )
    overall_current_count = dict(
        overall_current.values("equipment_id").annotate(c=Count("booking_id")).values_list("equipment_id", "c")
    )

    # Distinct users & samples & booking hours (served bookings)
    user_sets: dict[int, set[int]] = {eid: set() for eid in eq_ids}
    user_int: dict[int, set[int]] = {eid: set() for eid in eq_ids}
    user_ext: dict[int, set[int]] = {eid: set() for eid in eq_ids}
    samples_total: dict[int, int] = defaultdict(int)
    samples_int: dict[int, int] = defaultdict(int)
    samples_ext: dict[int, int] = defaultdict(int)
    booking_minutes_total: dict[int, float] = defaultdict(float)
    booking_minutes_int: dict[int, float] = defaultdict(float)
    booking_minutes_ext: dict[int, float] = defaultdict(float)

    served_bookings = (
        bookings_served.filter(equipment_id__in=eq_ids)
        .values(
            "booking_id",
            "equipment_id",
            "user_id",
            "user_type_snapshot",
            "total_time_minutes",
            "input_values",
        )
        .iterator(chunk_size=500)
    )
    for row in served_bookings:
        eid = row["equipment_id"]
        uid = row["user_id"]
        ut = row["user_type_snapshot"]
        user_sets[eid].add(uid)
        if _is_internal_snapshot(ut):
            user_int[eid].add(uid)
        elif _is_external_snapshot(ut):
            user_ext[eid].add(uid)
        sc = _parse_sample_count_from_input_values(row["input_values"])
        samples_total[eid] += sc
        if _is_internal_snapshot(ut):
            samples_int[eid] += sc
        elif _is_external_snapshot(ut):
            samples_ext[eid] += sc
        tm = float(row["total_time_minutes"] or 0)
        booking_minutes_total[eid] += tm
        if _is_internal_snapshot(ut):
            booking_minutes_int[eid] += tm
        elif _is_external_snapshot(ut):
            booking_minutes_ext[eid] += tm

    completed_revenue_qs = completed_in_range.select_related("user", "user__department", "equipment")
    total_revenue = completed_revenue_qs.aggregate(total=Sum("total_charge"))["total"] or 0
    revenue_internal = 0.0
    revenue_external = 0.0
    for b in completed_revenue_qs.iterator(chunk_size=200):
        amt = float(b.total_charge or 0)
        if _is_external_snapshot(b.user_type_snapshot):
            revenue_external += amt
        else:
            revenue_internal += amt

    revenue_by_user_type = list(
        completed_revenue_qs.values("user_type_snapshot")
        .annotate(total=Sum("total_charge"), count=Count("booking_id"))
        .order_by("-total")
    )
    revenue_by_department = list(
        completed_revenue_qs.values("user__department__name")
        .annotate(total=Sum("total_charge"), count=Count("booking_id"))
        .order_by("-total")
    )
    revenue_by_user = list(
        completed_revenue_qs.values("user_id", "user__name", "user__email")
        .annotate(total=Sum("total_charge"), count=Count("booking_id"))
        .order_by("-total")[:200]
    )
    revenue_by_equipment = list(
        completed_revenue_qs.values("equipment_id", "equipment__code", "equipment__name")
        .annotate(total=Sum("total_charge"), count=Count("booking_id"))
        .order_by("-total")
    )
    ext_rev_q = Q()
    for code in ("external", "RND", "rnd", "Industry", "industry", "other", "Other"):
        ext_rev_q |= Q(user_type_snapshot__iexact=code)
    revenue_by_external_category = list(
        completed_revenue_qs.filter(ext_rev_q)
        .values("user_type_snapshot")
        .annotate(total=Sum("total_charge"), count=Count("booking_id"))
        .order_by("-total")
    )

    equipment_payload = []
    sum_avail_working = 0.0
    sum_completed_working = 0.0

    for eq in equipment_list:
        eid = eq.equipment_id
        slot_s = eq_slot_stats.get(eid, {})
        perf = eq_perf_slots[eid]
        avail_w = float(perf["available_hours_working_window"])
        comp_w = float(perf["completed_hours_working_window"])
        sum_avail_working += avail_w
        sum_completed_working += comp_w
        util_working = round((comp_w / avail_w) if avail_w > 0 else 0.0, 4)

        managers_payload = []
        for em in eq.equipment_managers.all():
            u = em.manager
            if u:
                disp = (getattr(u, "name", None) or "").strip()
                if not disp and callable(getattr(u, "get_full_name", None)):
                    disp = (u.get_full_name() or "").strip()
                managers_payload.append(
                    {
                        "id": u.id,
                        "name": disp,
                        "email": getattr(u, "email", "") or "",
                    }
                )
        operators_payload = []
        for eo in eq.equipment_operators.all():
            u = eo.operator
            if u:
                disp = (getattr(u, "name", None) or "").strip()
                if not disp and callable(getattr(u, "get_full_name", None)):
                    disp = (u.get_full_name() or "").strip()
                operators_payload.append(
                    {
                        "id": u.id,
                        "name": disp,
                        "email": getattr(u, "email", "") or "",
                    }
                )

        ratings = _aggregate_ratings_for_equipment(eid, start, end)

        slot_window_label_parts = []
        if eq.weekly_view_time_from:
            slot_window_label_parts.append(eq.weekly_view_time_from.strftime("%H:%M"))
        if eq.weekly_view_time_to:
            slot_window_label_parts.append(eq.weekly_view_time_to.strftime("%H:%M"))
        slot_window_label = (
            " – ".join(slot_window_label_parts) if slot_window_label_parts else "Full day (no window limit)"
        )

        equipment_payload.append(
            {
                "equipment_id": eid,
                "name": eq.name or "",
                "code": eq.code or "",
                "status": getattr(eq, "status", None),
                "status_display": eq.get_status_display() if hasattr(eq, "get_status_display") else getattr(eq, "status", None),
                "officers_in_charge": managers_payload,
                "lab_operators": operators_payload,
                "slot_window_display": slot_window_label,
                "distinct_users_served": len(user_sets.get(eid, set())),
                "distinct_users_internal": len(user_int.get(eid, set())),
                "distinct_users_external": len(user_ext.get(eid, set())),
                "total_samples": int(samples_total.get(eid, 0)),
                "samples_internal": int(samples_int.get(eid, 0)),
                "samples_external": int(samples_ext.get(eid, 0)),
                "total_booking_hours": round(booking_minutes_total.get(eid, 0) / 60.0, 2),
                "booking_hours_internal": round(booking_minutes_int.get(eid, 0) / 60.0, 2),
                "booking_hours_external": round(booking_minutes_ext.get(eid, 0) / 60.0, 2),
                "available_hours_working_window": round(avail_w, 2),
                "available_hours_weekend_or_holiday": round(float(perf["available_hours_weekend_or_holiday"]), 2),
                "completed_slot_hours_working_window": round(comp_w, 2),
                "utilization_vs_working_capacity": util_working,
                "blocked_hours": round(float(perf["blocked_hours"]), 2),
                "other_disruption_hours": round(float(perf["other_disruption_hours"]), 2),
                "total_bookings_in_period": in_range_count.get(eid, 0),
                "completed_in_period": completed_in_range_count.get(eid, 0),
                "overall_bookings": overall_count.get(eid, 0),
                "overall_current_bookings": overall_current_count.get(eid, 0),
                "under_maintenance_slots": int(slot_s.get("under_maintenance_slots", 0)),
                "under_maintenance_hours": round(float(slot_s.get("under_maintenance_hours", 0)), 2),
                "operator_absent_slots": int(slot_s.get("operator_absent_slots", 0)),
                "operator_absent_hours": round(float(slot_s.get("operator_absent_hours", 0)), 2),
                "booking_not_utilized_slots": int(slot_s.get("booking_not_utilized_slots", 0)),
                "booking_not_utilized_hours": round(float(slot_s.get("booking_not_utilized_hours", 0)), 2),
                "no_booking_slots": int(slot_s.get("no_booking_slots", 0)),
                "no_booking_hours": round(float(slot_s.get("no_booking_hours", 0)), 2),
                "booked_slots": int(slot_s.get("booked_slots", 0)),
                "booked_hours": round(float(slot_s.get("booked_hours", 0)), 2),
                "user_ratings": ratings,
            }
        )

    total_um = sum(float(s.get("under_maintenance_hours", 0)) for s in eq_slot_stats.values())
    total_oa = sum(float(s.get("operator_absent_hours", 0)) for s in eq_slot_stats.values())
    total_nu = sum(float(s.get("booking_not_utilized_hours", 0)) for s in eq_slot_stats.values())
    total_nob = sum(float(s.get("no_booking_hours", 0)) for s in eq_slot_stats.values())
    total_booked = sum(float(s.get("booked_hours", 0)) for s in eq_slot_stats.values())
    total_hours = float(total_um + total_oa + total_nu + total_nob + total_booked)
    downtime_hours = float(total_um + total_oa)
    utilized_hours = float(total_booked)
    utilization_factor = round((utilized_hours / total_hours) if total_hours > 0 else 0.0, 4)

    utilization_pie = [
        {"name": "Utilized (Booked)", "value": round(total_booked, 2), "hours": round(total_booked, 2)},
        {"name": "Booking not utilized", "value": round(total_nu, 2), "hours": round(total_nu, 2)},
        {"name": "Under maintenance", "value": round(total_um, 2), "hours": round(total_um, 2)},
        {"name": "Operator absent", "value": round(total_oa, 2), "hours": round(total_oa, 2)},
        {"name": "No booking", "value": round(total_nob, 2), "hours": round(total_nob, 2)},
    ]
    utilization_pie = [p for p in utilization_pie if p["hours"] > 0]
    if not utilization_pie:
        utilization_pie = [{"name": "No slot data", "value": 0, "hours": 0}]

    util_vs_working_global = (
        round((sum_completed_working / sum_avail_working) if sum_avail_working > 0 else 0.0, 4)
    )

    _today = timezone.localdate()
    human_range = f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"
    duration_suffix = " (till current date)" if end == _today else ""

    # Department name for letterhead: show if all equipment in the report belong to the same internal department.
    department_name = ""
    try:
        dept_names = set()
        for eq in equipment_list:
            d = getattr(eq, "internal_department", None)
            n = (getattr(d, "name", None) or "").strip() if d else ""
            if n:
                dept_names.add(n)
        if len(dept_names) == 1:
            department_name = list(dept_names)[0]
        elif len(dept_names) > 1:
            department_name = "Multiple Departments"
    except Exception:
        department_name = ""

    return {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "report_header": {
            "institute_name": "Institute Instrumentation Centre",
            "department_name": department_name,
            "organization": "Indian Institute of Technology Roorkee",
            "report_title": _report_title_for_equipment("Equipment Performance Report", equipment_list),
            "period_display": human_range,
            "month_year": human_range,
            "report_duration_suffix": duration_suffix,
        },
        "equipment": equipment_payload,
        "utilization_pie": utilization_pie,
        "summary": {
            "total_equipment": len(equipment_list),
            "total_hours": round(total_hours, 2),
            "utilized_hours": round(utilized_hours, 2),
            "downtime_hours": round(downtime_hours, 2),
            "utilization_factor": utilization_factor,
            "revenue_total": float(total_revenue or 0),
            "revenue_internal": float(revenue_internal or 0),
            "revenue_external": float(revenue_external or 0),
            "available_hours_working_window": round(sum_avail_working, 2),
            "completed_hours_in_working_window": round(sum_completed_working, 2),
            "utilization_vs_working_capacity": util_vs_working_global,
        },
        "financial": {
            "revenue_by_user_type": revenue_by_user_type,
            "revenue_by_department": revenue_by_department,
            "revenue_by_user": revenue_by_user,
            "revenue_by_equipment": revenue_by_equipment,
            "revenue_by_external_category": revenue_by_external_category,
        },
    }
