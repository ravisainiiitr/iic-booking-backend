"""
Equipment utilization, performance metrics, and OIC report data.

Computes per-equipment stats (users, samples, hours, availability windows, ratings)
for a date range. Used by admin report API, PDF/Excel exports, and monthly emails.
"""

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from django.db.models import Count, Prefetch, Q, Sum
from django.utils import timezone

from iic_booking.users.display import apply_faculty_name_prefix, get_user_display_name, name_with_honorific
from iic_booking.users.models.user_type import UserType

from .models import (
    Booking,
    BookingStatus,
    DailySlot,
    Equipment,
    EquipmentManager,
    EquipmentOperator,
    SlotStatus,
)
from .utilization import (
    AVAILABLE_SLOT_STATUSES,
    UtilizationTally,
    utilization_period,
    utilization_ratio,
    utilization_scope,
    window_hours,
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
    from .report_revenue import is_external_snapshot

    return is_external_snapshot(ut)


# Bookings counted as "served" for user/sample/hour stats (had slots in period). Fully refunded
# outcomes (operator unavailable, maintenance / disruption cancellations) served nobody.
_SERVED_EXCLUDE_STATUSES = frozenset(
    {
        BookingStatus.CANCELLED,
        BookingStatus.REFUNDED,
        BookingStatus.WAITLISTED,
        BookingStatus.PENDING,
        BookingStatus.ABSENT,
        BookingStatus.UNDER_MAINTENANCE,
        BookingStatus.OTHER_DISRUPTION,
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
    """Lab Operator emails for equipment."""
    rows = EquipmentOperator.objects.filter(equipment_id=equipment_id).select_related("operator")
    return [r.operator.email for r in rows if r.operator and getattr(r.operator, "email", None)]


def get_oic_emails_for_equipment(equipment_id: int) -> list[str]:
    """Return list of OIC (manager) emails for the given equipment."""
    managers = EquipmentManager.objects.filter(equipment_id=equipment_id).select_related("manager")
    return [m.manager.email for m in managers if m.manager and getattr(m.manager, "email", None)]


def get_booking_confirmation_email_opt_out_user_ids(equipment) -> set[int]:
    """
    OIC / Lab Operator user ids who switched off booking confirmation emails for this equipment.
    A user assigned in several roles is opted out only when every one of their rows is opted out.
    """
    if equipment is None:
        return set()
    eid = getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)
    if eid is None:
        return set()
    opted_out: set[int] = set()
    keep: set[int] = set()
    for uid, flag in EquipmentManager.objects.filter(equipment_id=eid).values_list(
        "manager_id", "disable_booking_confirmation_email"
    ):
        (opted_out if flag else keep).add(uid)
    for uid, flag in EquipmentOperator.objects.filter(equipment_id=eid).values_list(
        "operator_id", "disable_booking_confirmation_email"
    ):
        (opted_out if flag else keep).add(uid)
    return opted_out - keep


def get_equipment_staff_notify_users(equipment) -> list:
    """
    Active Officer In Charge (managers), temporary OIC, and Lab Operator
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
    for row in EquipmentTemporaryOIC.objects.active().filter(equipment_id=eid).select_related("temporary_oic"):
        _add(row.temporary_oic)
    for eo in EquipmentOperator.objects.filter(equipment_id=eid).select_related("operator"):
        _add(eo.operator)
    return out


def _active_unique_users(users) -> list:
    seen: set[int] = set()
    out: list = []
    for u in users:
        if not u or not getattr(u, "id", None) or u.id in seen:
            continue
        if getattr(u, "is_active", True) is False:
            continue
        seen.add(u.id)
        out.append(u)
    return out


def get_equipment_oic_users(equipment) -> list:
    """Active Officer In Charge (managers) and current temporary OIC for this equipment."""
    if equipment is None:
        return []
    eid = getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)
    if eid is None:
        return []
    from iic_booking.equipment.models import EquipmentTemporaryOIC

    managers = [
        em.manager for em in EquipmentManager.objects.filter(equipment_id=eid).select_related("manager")
    ]
    temporary = [
        row.temporary_oic
        for row in EquipmentTemporaryOIC.objects.active().filter(equipment_id=eid).select_related("temporary_oic")
    ]
    return _active_unique_users(managers + temporary)


def get_equipment_lab_incharge_users(equipment) -> list:
    """Active Lab Operator assigned to this equipment."""
    if equipment is None:
        return []
    eid = getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)
    if eid is None:
        return []
    return _active_unique_users(
        eo.operator for eo in EquipmentOperator.objects.filter(equipment_id=eid).select_related("operator")
    )


def get_equipment_ids_managed_by_oic(user_id: int) -> list[int]:
    """Return equipment IDs for which the user is OIC (manager) or an active OIC substitute (temporary OIC)."""
    from iic_booking.equipment.models import EquipmentTemporaryOIC

    primary_ids = set(
        EquipmentManager.objects.filter(manager_id=user_id).values_list("equipment_id", flat=True)
    )
    temp_ids = set(
        EquipmentTemporaryOIC.objects.active()
        .filter(temporary_oic_id=user_id)
        .values_list("equipment_id", flat=True)
    )
    return list(primary_ids | temp_ids)


def get_equipment_report_data(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    equipment_ids: Optional[list[int]] = None,
    include_revenue: bool = True,
) -> dict[str, Any]:
    """
    Compute per-equipment performance stats and overall utilization for the given date range.

    With ``include_revenue=False`` (Lab Operators) the revenue summary keys and ``financial`` are omitted.
    """
    payload = _equipment_report_payload(date_from, date_to, equipment_ids, include_revenue)
    payload["revenue_visible"] = include_revenue
    if not include_revenue:
        from .charge_visibility import EQUIPMENT_REPORT_SUMMARY_MONEY_KEYS

        for key in EQUIPMENT_REPORT_SUMMARY_MONEY_KEYS:
            payload.get("summary", {}).pop(key, None)
        payload.pop("financial", None)
    return payload


def report_equipment_scope(equipment_ids: Optional[list[int]]):
    """``(equipment_list, equipment_by_id, eq_ids, mode_rollup)`` of a report.

    ``equipment_list``: report rows (parent or standalone equipment, managers / operators prefetched);
    ``eq_ids``: those rows plus their modes, test equipment left out; ``mode_rollup``: mode id → parent id.
    """
    from .testdata import exclude_test_equipment, exclude_test_users, get_test_equipment_ids

    # Test equipment (and its modes) is left out of every figure.
    qs_equipment = exclude_test_equipment(Equipment.objects.all()).order_by("code")
    if equipment_ids is not None:
        qs_equipment = qs_equipment.filter(equipment_id__in=equipment_ids)

    equipment_list = list(
        qs_equipment.prefetch_related(
            Prefetch(
                "equipment_managers",
                queryset=exclude_test_users(EquipmentManager.objects.select_related("manager"), prefix="manager__"),
            ),
            Prefetch(
                "equipment_operators",
                queryset=exclude_test_users(EquipmentOperator.objects.select_related("operator"), prefix="operator__"),
            ),
        )
    )
    original_eq_ids = [e.equipment_id for e in equipment_list]
    from .mode_utils import expand_equipment_ids_for_mode_rollup

    eq_ids, mode_rollup = expand_equipment_ids_for_mode_rollup(original_eq_ids)
    test_ids = get_test_equipment_ids(eq_ids)
    eq_ids = [eid for eid in eq_ids if eid not in test_ids]
    # Report rows are parent (or standalone) only; children fold into parent utilization.
    payload_eq_ids = sorted({mode_rollup.get(eid, eid) for eid in original_eq_ids})
    if set(eq_ids) != set(original_eq_ids):
        equipment_by_id = {
            e.equipment_id: e
            for e in Equipment.objects.filter(equipment_id__in=eq_ids).prefetch_related(
                Prefetch(
                    "equipment_managers",
                    queryset=exclude_test_users(EquipmentManager.objects.select_related("manager"), prefix="manager__"),
                ),
                Prefetch(
                    "equipment_operators",
                    queryset=exclude_test_users(
                        EquipmentOperator.objects.select_related("operator"), prefix="operator__"
                    ),
                ),
            )
        }
        equipment_list = [equipment_by_id[eid] for eid in payload_eq_ids if eid in equipment_by_id]
    else:
        equipment_by_id = {e.equipment_id: e for e in equipment_list}
        equipment_list = [equipment_by_id[eid] for eid in payload_eq_ids if eid in equipment_by_id]
    return equipment_list, equipment_by_id, eq_ids, mode_rollup


def _equipment_report_payload(
    date_from: Optional[str],
    date_to: Optional[str],
    equipment_ids: Optional[list[int]],
    include_revenue: bool,
) -> dict[str, Any]:
    from .report_revenue import report_period

    start, end = report_period(date_from, date_to)

    # Slot-hour figures (utilization, slot outcomes, downtime, capacity) start at the portal go-live date.
    slot_period = utilization_period(start, end)

    equipment_list, equipment_by_id, eq_ids, mode_rollup = report_equipment_scope(equipment_ids)
    payload_eq_ids = [e.equipment_id for e in equipment_list]

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
                "utilization_period_display": slot_period.display(),
                "utilization_period_note": slot_period.note(),
            },
            "equipment": [],
            "utilization_pie": [{"name": "No data", "value": 0, "hours": 0}],
            "summary": {
                "total_equipment": 0,
                "total_hours": 0.0,
                "utilized_hours": 0.0,
                "downtime_hours": 0.0,
                "disruption_hours": 0.0,
                "utilization_factor": None,
                "utilization_booked_hours": 0.0,
                "utilization_available_hours": 0.0,
                "booked_hours_outside_window": 0.0,
                **slot_period.as_dict(),
                "revenue_total": 0.0,
                "revenue_internal": 0.0,
                "revenue_external": 0.0,
                "available_hours_working_window": 0.0,
                "completed_hours_in_working_window": 0.0,
                "utilization_vs_working_capacity": None,
            },
            "financial": {
                "revenue_by_user_type": [],
                "revenue_by_department": [],
                "revenue_by_user": [],
                "revenue_by_equipment": [],
                "revenue_by_external_category": [],
                "revenue_by_month": [],
            },
        }

    scope = utilization_scope(eq_ids, start, end, period=slot_period)

    eq_slot_stats: dict[int, dict[str, float | int]] = {}
    eq_perf_slots: dict[int, dict[str, float]] = defaultdict(
        lambda: {
            "blocked_hours": 0.0,
            "other_disruption_hours": 0.0,
        }
    )
    utilization: dict[int, UtilizationTally] = defaultdict(UtilizationTally)

    for eid in eq_ids:
        rollup_eid = mode_rollup.get(eid, eid)
        if rollup_eid not in eq_slot_stats:
            eq_slot_stats[rollup_eid] = {
                "under_maintenance_slots": 0,
                "under_maintenance_hours": 0.0,
                "scheduled_maintenance_slots": 0,
                "scheduled_maintenance_hours": 0.0,
                "other_reasons_slots": 0,
                "other_reasons_hours": 0.0,
                "operator_absent_slots": 0,
                "operator_absent_hours": 0.0,
                "booking_not_utilized_slots": 0,
                "booking_not_utilized_hours": 0.0,
                "no_booking_slots": 0,
                "no_booking_hours": 0.0,
                "booked_slots": 0,
                "booked_hours": 0.0,
            }

    # Other Reasons blocks count as a disruption only when staff recorded them as one (holiday, training,
    # repeat-rule and legacy blocks share the BLOCKED status).
    from .models import DisruptionEventSlot, DisruptionType

    other_reason_slot_ids = set(
        DisruptionEventSlot.objects.filter(
            event__disruption_type=DisruptionType.OTHER,
            event__is_deleted=False,
            released_at__isnull=True,
            daily_slot__date__gte=slot_period.start,
            daily_slot__date__lte=slot_period.end,
            daily_slot__slot_master__equipment_id__in=eq_ids,
        ).values_list("daily_slot_id", flat=True)
    )

    for slot in scope.slots():
        eid = slot.target_id
        if eid not in eq_slot_stats:
            continue
        st = slot.status
        # Test-account bookings must not affect utilization / disruption stats.
        is_test_bk = slot.test_booking
        # Every slot-hour figure is the slot's time inside the weekly view window on working days, so the
        # status breakdown below adds up to the utilization's available hours.
        hrs = scope.add(utilization, slot)
        if st not in AVAILABLE_SLOT_STATUSES:
            hrs = window_hours(slot.start, slot.end, slot.window, scope.calendar)[0]

        if st == SlotStatus.UNDER_MAINTENANCE:
            eq_slot_stats[eid]["under_maintenance_slots"] += 1
            eq_slot_stats[eid]["under_maintenance_hours"] += hrs
        elif st == SlotStatus.SCHEDULED_MAINTENANCE:
            eq_slot_stats[eid]["scheduled_maintenance_slots"] += 1
            eq_slot_stats[eid]["scheduled_maintenance_hours"] += hrs
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
            if slot.slot_id in other_reason_slot_ids:
                eq_slot_stats[eid]["other_reasons_slots"] += 1
                eq_slot_stats[eid]["other_reasons_hours"] += hrs
        elif st == SlotStatus.NOT_AVAILABLE:
            eq_perf_slots[eid]["blocked_hours"] += hrs

        if slot.booking_id and not is_test_bk and slot.booking_status == BookingStatus.OTHER_DISRUPTION:
            eq_perf_slots[eid]["other_disruption_hours"] += hrs

    from iic_booking.users.test_accounts import exclude_test_bookings

    from .report_revenue import bookings_in_report_range, report_revenue_bookings

    bookings_in_range = bookings_in_report_range(start, end, eq_ids)
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

    def _roll_counts(raw: dict) -> dict[int, int]:
        out: dict[int, int] = defaultdict(int)
        for eid, c in raw.items():
            out[mode_rollup.get(eid, eid)] += int(c or 0)
        return dict(out)

    in_range_count = _roll_counts(in_range_count)
    completed_in_range_count = _roll_counts(completed_in_range_count)
    overall_count = _roll_counts(overall_count)
    overall_current_count = _roll_counts(overall_current_count)

    # Distinct users & samples & booking hours (served bookings)
    user_sets: dict[int, set[int]] = {eid: set() for eid in payload_eq_ids}
    user_int: dict[int, set[int]] = {eid: set() for eid in payload_eq_ids}
    user_ext: dict[int, set[int]] = {eid: set() for eid in payload_eq_ids}
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
            "equipment__profile_type",
        )
        .iterator(chunk_size=500)
    )
    from .fabrication import job_quantity_from_values
    from .models import FABRICATION_PROFILE_TYPES

    for row in served_bookings:
        eid = mode_rollup.get(row["equipment_id"], row["equipment_id"])
        if eid not in user_sets:
            user_sets[eid] = set()
            user_int[eid] = set()
            user_ext[eid] = set()
        uid = row["user_id"]
        ut = row["user_type_snapshot"]
        user_sets[eid].add(uid)
        if _is_internal_snapshot(ut):
            user_int[eid].add(uid)
        elif _is_external_snapshot(ut):
            user_ext[eid].add(uid)
        profile_type = row["equipment__profile_type"]
        if profile_type in FABRICATION_PROFILE_TYPES:
            # A is Quantity Required (older 3D print bookings stored the weight there).
            sc = job_quantity_from_values(profile_type, row["input_values"])
        else:
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

    revenue = (
        _revenue_tables(report_revenue_bookings(start, end, eq_ids), mode_rollup, equipment_by_id)
        if include_revenue
        else _revenue_tables(None, mode_rollup, equipment_by_id)
    )
    total_revenue = revenue["total"]
    revenue_internal = revenue["internal"]
    revenue_external = revenue["external"]

    equipment_payload = []

    for eq in equipment_list:
        eid = eq.equipment_id
        slot_s = eq_slot_stats.get(eid, {})
        perf = eq_perf_slots[eid]
        util = utilization[eid]

        managers_payload = []
        for em in eq.equipment_managers.all():
            u = em.manager
            if u:
                disp = get_user_display_name(u, fallback_to_email=False)
                if not disp and callable(getattr(u, "get_full_name", None)):
                    disp = (u.get_full_name() or "").strip()
                disp = name_with_honorific(u, em.honorific, default=disp)
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
                disp = get_user_display_name(u, fallback_to_email=False)
                if not disp and callable(getattr(u, "get_full_name", None)):
                    disp = (u.get_full_name() or "").strip()
                disp = name_with_honorific(u, eo.honorific, default=disp)
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
                "available_hours_working_window": round(util.available_hours, 2),
                "available_hours_weekend_or_holiday": round(util.off_day_hours, 2),
                "completed_slot_hours_working_window": round(util.completed_hours, 2),
                "utilization_vs_working_capacity": utilization_ratio(util.completed_hours, util.available_hours),
                "utilization_factor": util.factor,
                "utilization_booked_hours": round(util.booked_hours, 2),
                "utilization_available_hours": round(util.available_hours, 2),
                "booked_hours_outside_window": round(util.booked_hours_outside_window, 2),
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
                "scheduled_maintenance_slots": int(slot_s.get("scheduled_maintenance_slots", 0)),
                "scheduled_maintenance_hours": round(float(slot_s.get("scheduled_maintenance_hours", 0)), 2),
                "other_reasons_slots": int(slot_s.get("other_reasons_slots", 0)),
                "other_reasons_hours": round(float(slot_s.get("other_reasons_hours", 0)), 2),
                "disruption_hours": round(
                    float(slot_s.get("under_maintenance_hours", 0))
                    + float(slot_s.get("scheduled_maintenance_hours", 0))
                    + float(slot_s.get("operator_absent_hours", 0))
                    + float(slot_s.get("other_reasons_hours", 0)),
                    2,
                ),
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
    total_sm = sum(float(s.get("scheduled_maintenance_hours", 0)) for s in eq_slot_stats.values())
    total_oa = sum(float(s.get("operator_absent_hours", 0)) for s in eq_slot_stats.values())
    total_or = sum(float(s.get("other_reasons_hours", 0)) for s in eq_slot_stats.values())
    total_nu = sum(float(s.get("booking_not_utilized_hours", 0)) for s in eq_slot_stats.values())
    total_nob = sum(float(s.get("no_booking_hours", 0)) for s in eq_slot_stats.values())
    total_booked = sum(float(s.get("booked_hours", 0)) for s in eq_slot_stats.values())
    # Equals the utilization's available hours (same clipping, same statuses).
    total_hours = float(total_um + total_sm + total_oa + total_nu + total_nob + total_booked)
    # Downtime: maintenance (unplanned + scheduled) and operator absent. Disruption adds Other Reasons.
    downtime_hours = float(total_um + total_sm + total_oa)
    disruption_hours = float(downtime_hours + total_or)
    utilized_hours = float(total_booked)
    util_total = UtilizationTally()
    for eq in equipment_list:
        util_total.merge(utilization[eq.equipment_id])
    utilization_factor = utilization_ratio(util_total.booked_hours, util_total.available_hours)

    utilization_pie = [
        {"name": "Utilized (Booked)", "value": round(total_booked, 2), "hours": round(total_booked, 2)},
        {"name": "Booking not utilized", "value": round(total_nu, 2), "hours": round(total_nu, 2)},
        {"name": "Under maintenance", "value": round(total_um, 2), "hours": round(total_um, 2)},
        {"name": "Scheduled maintenance", "value": round(total_sm, 2), "hours": round(total_sm, 2)},
        {"name": "Operator absent", "value": round(total_oa, 2), "hours": round(total_oa, 2)},
        {"name": "No booking", "value": round(total_nob, 2), "hours": round(total_nob, 2)},
    ]
    utilization_pie = [p for p in utilization_pie if p["hours"] > 0]
    if not utilization_pie:
        utilization_pie = [{"name": "No slot data", "value": 0, "hours": 0}]

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
            "utilization_period_display": slot_period.display(),
            "utilization_period_note": slot_period.note(),
        },
        "equipment": equipment_payload,
        "utilization_pie": utilization_pie,
        "summary": {
            "total_equipment": len(equipment_list),
            "total_hours": round(total_hours, 2),
            "utilized_hours": round(utilized_hours, 2),
            "downtime_hours": round(downtime_hours, 2),
            "disruption_hours": round(disruption_hours, 2),
            "utilization_factor": utilization_factor,
            "utilization_booked_hours": round(util_total.booked_hours, 2),
            "utilization_available_hours": round(util_total.available_hours, 2),
            "booked_hours_outside_window": round(util_total.booked_hours_outside_window, 2),
            **slot_period.as_dict(),
            "revenue_total": float(total_revenue or 0),
            "revenue_internal": float(revenue_internal or 0),
            "revenue_external": float(revenue_external or 0),
            "available_hours_working_window": round(util_total.available_hours, 2),
            "completed_hours_in_working_window": round(util_total.completed_hours, 2),
            "utilization_vs_working_capacity": utilization_ratio(util_total.completed_hours, util_total.available_hours),
        },
        "financial": revenue["financial"],
    }


def _revenue_tables(qs, mode_rollup: dict[int, int], equipment_by_id: dict) -> dict[str, Any]:
    """Revenue cards and tables, grouped from the same rows the drill-downs read (``report_revenue.revenue_rows``).

    Every row carries ``drill_value``: the value of its drill-down segment (dimension = the table's dimension).
    """
    from django.contrib.auth import get_user_model

    from .report_revenue import NO_DEPARTMENT, NOT_RECORDED, group, money, month_label, revenue_rows, row_category_label

    rows = revenue_rows(qs, mode_rollup) if qs is not None else []
    total = sum((r.charge for r in rows), Decimal("0"))
    external = sum((r.charge for r in rows if r.external), Decimal("0"))

    def _row(bucket, **extra):
        return {**extra, "total": money(bucket.total), "count": bucket.count, "users": len(bucket.user_ids)}

    def _category_row(bucket):
        label = row_category_label(bucket.key)
        return _row(
            bucket, category=bucket.key, user_type_snapshot=label, user_type_label=label, drill_value=bucket.key
        )

    # Rows are user categories ("IITR Students" covers IITR and individual students); drill dimension "category".
    by_user_type = [_category_row(b) for b in group(rows, lambda r: r.category)]
    by_external = [_category_row(b) for b in group([r for r in rows if r.external], lambda r: r.category)]
    dept_names = {r.department_id: r.department_name for r in rows}
    by_department = [
        _row(
            b,
            user__department_id=b.key,
            user__department__name=dept_names.get(b.key) or None,
            drill_value=b.key if b.key is not None else NO_DEPARTMENT,
        )
        for b in group(rows, lambda r: r.department_id)
    ]
    by_equipment = []
    for b in group(rows, lambda r: r.family_id):
        eq = equipment_by_id.get(b.key)
        if eq is None:
            eq = Equipment.objects.filter(pk=b.key).only("code", "name").first()
        by_equipment.append(
            _row(
                b,
                equipment_id=b.key,
                equipment__code=getattr(eq, "code", "") or "",
                equipment__name=getattr(eq, "name", "") or "",
                drill_value=b.key,
            )
        )
    user_buckets = group(rows, lambda r: r.user_id)[:200]
    people = {
        u["id"]: u
        for u in get_user_model()
        .objects.filter(pk__in=[b.key for b in user_buckets])
        .values("id", "name", "email", "user_type")
    }
    by_user = []
    for b in user_buckets:
        u = people.get(b.key) or {}
        by_user.append(
            _row(
                b,
                user_id=b.key,
                user__name=apply_faculty_name_prefix(u.get("name"), u.get("user_type")),
                user__email=u.get("email") or "",
                drill_value=b.key,
            )
        )
    by_month = sorted(
        (
            _row(b, month=b.key, month_label=month_label(b.key), drill_value=b.key or NOT_RECORDED)
            for b in group(rows, lambda r: r.month)
        ),
        key=lambda r: r["month"] or "9999",
    )
    return {
        "total": money(total),
        "internal": money(total - external),
        "external": money(external),
        "financial": {
            "revenue_by_user_type": by_user_type,
            "revenue_by_department": by_department,
            "revenue_by_user": by_user,
            "revenue_by_equipment": by_equipment,
            "revenue_by_external_category": by_external,
            "revenue_by_month": by_month,
        },
    }
