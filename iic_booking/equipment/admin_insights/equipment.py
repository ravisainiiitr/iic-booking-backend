"""Equipment overview behind the dashboard's Equipment card.

Counts use the card's definition (``_equipment_status``): every equipment record in scope (modes of multi-mode
instruments are records of their own), Disposed left out of the total. Operational = ACTIVE; Under maintenance =
REPAIR / MAINTENANCE / INACTIVE; anything else (including no status) = Other. Disposed equipment is listed only when
the status filter asks for it, so the unfiltered list adds up to the card.

Per equipment, in a fixed number of queries:
- Down since / last status change: whole-equipment disruptions (not deleted) — the open one's start, and the latest
  start or end.
- Upcoming bookings: bookings (test accounts excluded) awaiting payment, pending, held or booked with a slot that
  has not started yet.
- 30-day utilisation: the Reports definition — booked slot hours ÷ all slot hours (booked, not utilized, available,
  under maintenance, scheduled maintenance, operator absent and Other Reasons blocks); test-account slots excluded.
  Empty when the equipment had no such slots (e.g. modes that share their parent's calendar).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from django.db.models import Count, DurationField, ExpressionWrapper, F, Max, Min, Q, Sum, Value
from django.db.models.functions import Coalesce, Upper
from django.utils import timezone

from iic_booking.equipment.admin_dashboard_summary import (
    MAINTENANCE_STATUSES,
    OPERATIONAL_STATUSES,
    _equipment_status,
    _Scope,
)

from .common import iso, multi, int_values, page_meta, page_params, scope_payload, share

UTILISATION_DAYS = 30
UPCOMING_STATUSES = ("PENDING", "PENDING_PAYMENT", "HOLD", "BOOKED")

STATUS_GROUPS = {
    "operational": "Operational",
    "under_maintenance": "Under maintenance",
    "other": "Other",
    "disposed": "Disposed",
}
SORTS = {
    "name": lambda r: (r["name"] or "").lower(),
    "code": lambda r: (r["code"] or "").lower(),
    "status": lambda r: (list(STATUS_GROUPS).index(r["status_group"]), (r["name"] or "").lower()),
    "utilisation": lambda r: (r["utilisation"] is None, r["utilisation"] or 0),
    "upcoming": lambda r: r["upcoming_bookings"],
    "down_since": lambda r: (r["down_since"] is None, r["down_since"] or ""),
    "last_change": lambda r: (r["last_status_change"] is None, r["last_status_change"] or ""),
}


def status_group(code: str | None) -> str:
    code = (code or "").upper()
    if code in OPERATIONAL_STATUSES:
        return "operational"
    if code in MAINTENANCE_STATUSES:
        return "under_maintenance"
    if code == "DISPOSED":
        return "disposed"
    return "other"


def _status_q(values: list[str]) -> Q | None:
    """Status filter: group keys (``operational`` …) or raw codes; ``None`` = everything except Disposed."""
    if not values:
        return None
    if any(v.strip().lower() == "all" for v in values):
        return Q()
    q = Q(pk__in=[])
    for value in values:
        key = value.strip().lower()
        if key == "operational":
            q |= Q(status_code__in=OPERATIONAL_STATUSES)
        elif key == "under_maintenance":
            q |= Q(status_code__in=MAINTENANCE_STATUSES)
        elif key == "disposed":
            q |= Q(status_code="DISPOSED")
        elif key == "other":
            q |= ~Q(status_code__in=(*OPERATIONAL_STATUSES, *MAINTENANCE_STATUSES, "DISPOSED"))
        else:
            q |= Q(status_code=value.strip().upper())
    return q


def _base(scope: _Scope):
    from iic_booking.equipment.models import Equipment

    qs = scope.by_department(Equipment.objects.all(), "internal_department_id").order_by()
    return qs.annotate(status_code=Upper(Coalesce("status", Value(""))))


def _filtered(scope: _Scope, params):
    qs = _base(scope)
    status_q = _status_q(multi(params, "status"))
    qs = qs.exclude(status_code="DISPOSED") if status_q is None else qs.filter(status_q)
    categories = multi(params, "category")
    if categories:
        q = Q(category_id__in=int_values(categories))
        if "none" in categories:
            q |= Q(category__isnull=True)
        qs = qs.filter(q)
    departments = int_values(multi(params, "department"))
    if departments and scope.is_institute:
        qs = qs.filter(internal_department_id__in=departments)
    profile_types = [p.upper() for p in multi(params, "profile_type")]
    if profile_types:
        q = Q(profile_type__in=[p for p in profile_types if p != "NONE"])
        if "NONE" in profile_types:
            q |= Q(profile_type__isnull=True) | Q(profile_type="")
        qs = qs.filter(q)
    oics = multi(params, "oic")
    if oics:
        q = Q(equipment_managers__manager_id__in=int_values(oics))
        if "none" in oics:
            q |= Q(equipment_managers__isnull=True)
        qs = qs.filter(pk__in=_base(scope).filter(q).values("pk"))
    test_only = str(params.get("test_only") or "").strip().lower()
    if test_only in ("1", "true", "yes"):
        qs = qs.filter(visible_to_test_accounts_only=True)
    elif test_only in ("0", "false", "no"):
        qs = qs.filter(visible_to_test_accounts_only=False)
    search = str(params.get("search") or "").strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search))
    return qs


def _managers(ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    from iic_booking.equipment.models import EquipmentManager
    from iic_booking.users.display import get_user_display_name, name_with_honorific

    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    rows = (
        EquipmentManager.objects.filter(equipment_id__in=ids)
        .select_related("manager")
        .order_by("equipment_id", "equipment_manager_id")
    )
    for em in rows:
        user = em.manager
        name = name_with_honorific(user, em.honorific, default=get_user_display_name(user))
        out[em.equipment_id].append({"id": user.pk, "name": name, "email": user.email or ""})
    return out


def _disruptions(ids: list[int]) -> tuple[dict[int, Any], dict[int, Any]]:
    from iic_booking.equipment.models import DisruptionEvent, DisruptionScope

    events = DisruptionEvent.objects.filter(equipment_id__in=ids, scope=DisruptionScope.EQUIPMENT, is_deleted=False)
    down_since = {
        r["equipment_id"]: r["since"]
        for r in events.filter(ended_at__isnull=True).order_by().values("equipment_id").annotate(since=Min("start_at"))
    }
    last_change = {}
    for r in events.order_by().values("equipment_id").annotate(started=Max("started_at"), ended=Max("ended_at")):
        last_change[r["equipment_id"]] = max(v for v in (r["started"], r["ended"]) if v is not None)
    return down_since, last_change


def _upcoming(ids: list[int], now) -> dict[int, dict[str, Any]]:
    from iic_booking.equipment.models import Booking

    rows = (
        Booking.objects.filter(
            equipment_id__in=ids,
            status__in=UPCOMING_STATUSES,
            user__is_test_account=False,
            daily_slots__start_datetime__gte=now,
        )
        .order_by()
        .values("equipment_id")
        .annotate(n=Count("pk", distinct=True), next_at=Min("daily_slots__start_datetime"))
    )
    return {r["equipment_id"]: {"count": r["n"] or 0, "next_at": r["next_at"]} for r in rows}


def _utilisation(ids: list[int], now) -> dict[int, dict[str, float]]:
    from iic_booking.equipment.models import DailySlot, DisruptionEventSlot, DisruptionType, SlotStatus

    today = timezone.localdate(now)
    start = today - timedelta(days=UTILISATION_DAYS - 1)
    other_reason_slots = DisruptionEventSlot.objects.filter(
        event__disruption_type=DisruptionType.OTHER,
        event__is_deleted=False,
        released_at__isnull=True,
        daily_slot__date__gte=start,
        daily_slot__date__lte=today,
    ).values("daily_slot_id")
    real = ~Q(booking__user__is_test_account=True)
    booked = Q(status=SlotStatus.BOOKED) & real
    counted = (
        booked
        | (Q(status=SlotStatus.BOOKING_NOT_UTILIZED) & real)
        | Q(
            status__in=(
                SlotStatus.AVAILABLE,
                SlotStatus.UNDER_MAINTENANCE,
                SlotStatus.SCHEDULED_MAINTENANCE,
                SlotStatus.OPERATOR_ABSENT,
            )
        )
        | (Q(status=SlotStatus.BLOCKED) & Q(pk__in=other_reason_slots))
    )
    duration = ExpressionWrapper(F("end_datetime") - F("start_datetime"), output_field=DurationField())
    rows = (
        DailySlot.objects.filter(slot_master__equipment_id__in=ids, date__gte=start, date__lte=today)
        .order_by()
        .values("slot_master__equipment_id")
        .annotate(booked=Sum(duration, filter=booked), total=Sum(duration, filter=counted))
    )
    out = {}
    for r in rows:
        total = r["total"].total_seconds() / 3600 if r["total"] else 0.0
        used = r["booked"].total_seconds() / 3600 if r["booked"] else 0.0
        out[r["slot_master__equipment_id"]] = {"booked_hours": round(used, 2), "slot_hours": round(total, 2)}
    return out


def _breakdown(counter: Counter, labels: dict, *, none_label: str) -> list[dict[str, Any]]:
    rows = [
        {"key": key if key is not None else "none", "label": labels.get(key) or none_label, "count": n}
        for key, n in counter.items()
    ]
    return sorted(rows, key=lambda r: (-r["count"], str(r["label"]).lower()))


def _options(scope: _Scope) -> dict[str, Any]:
    from iic_booking.equipment.models import EquipmentCategory, EquipmentManager, EquipmentProfileType
    from iic_booking.users.display import get_user_display_name

    base = _base(scope)
    ids = base.values("pk")
    categories = list(
        EquipmentCategory.objects.filter(equipment__in=ids).distinct().order_by("name").values("id", "name")
    )
    managers = {}
    for em in EquipmentManager.objects.filter(equipment_id__in=ids).select_related("manager"):
        managers[em.manager_id] = get_user_display_name(em.manager)
    options = {
        "statuses": [{"value": k, "label": v} for k, v in STATUS_GROUPS.items()],
        "categories": categories,
        "oics": [{"id": k, "name": v} for k, v in sorted(managers.items(), key=lambda kv: kv[1].lower())],
        "profile_types": [{"value": v, "label": str(label)} for v, label in EquipmentProfileType.choices],
        "departments": [],
    }
    if scope.is_institute:
        options["departments"] = list(
            base.exclude(internal_department__isnull=True)
            .values("internal_department_id", "internal_department__name")
            .distinct()
            .order_by("internal_department__name")
        )
        options["departments"] = [
            {"id": d["internal_department_id"], "name": d["internal_department__name"]} for d in options["departments"]
        ]
    return options


def build_equipment_insights(user, params) -> dict[str, Any]:
    from iic_booking.equipment.models import EquipmentProfileType, EquipmentStatus

    scope = _Scope(user)
    now = timezone.now()
    qs = _filtered(scope, params)
    records = list(
        qs.values(
            "equipment_id",
            "name",
            "code",
            "status",
            "profile_type",
            "category_id",
            "category__name",
            "internal_department_id",
            "internal_department__name",
            "parent_equipment_id",
            "parent_equipment__name",
            "visible_to_test_accounts_only",
        )
    )
    ids = [r["equipment_id"] for r in records]
    managers = _managers(ids)
    down_since, last_change = _disruptions(ids)
    upcoming = _upcoming(ids, now)
    usage = _utilisation(ids, now)
    status_labels = dict(EquipmentStatus.choices)
    profile_labels = dict(EquipmentProfileType.choices)

    rows = []
    by_status: Counter = Counter()
    by_category: Counter = Counter()
    by_department: Counter = Counter()
    by_profile: Counter = Counter()
    by_oic: Counter = Counter()
    category_names: dict = {}
    department_names: dict = {}
    oic_names: dict = {}
    booked_total = slot_total = 0.0
    for r in records:
        eid = r["equipment_id"]
        group = status_group(r["status"])
        oics = managers.get(eid, [])
        since = down_since.get(eid) if group != "operational" else None
        use = usage.get(eid)
        utilisation = share(use["booked_hours"], use["slot_hours"]) if use else None
        if use:
            booked_total += use["booked_hours"]
            slot_total += use["slot_hours"]
        up = upcoming.get(eid) or {}
        rows.append(
            {
                "equipment_id": eid,
                "name": r["name"] or "",
                "code": r["code"] or "",
                "status": r["status"] or "",
                "status_display": str(status_labels.get(r["status"], r["status"] or "Not set")),
                "status_group": group,
                "status_group_display": STATUS_GROUPS[group],
                "profile_type": r["profile_type"] or "",
                "profile_type_display": str(profile_labels.get(r["profile_type"], "") or "Not set"),
                "category": {"id": r["category_id"], "name": r["category__name"]} if r["category_id"] else None,
                "department": (
                    {"id": r["internal_department_id"], "name": r["internal_department__name"]}
                    if r["internal_department_id"]
                    else None
                ),
                "parent_equipment": (
                    {"id": r["parent_equipment_id"], "name": r["parent_equipment__name"]}
                    if r["parent_equipment_id"]
                    else None
                ),
                "test_only": bool(r["visible_to_test_accounts_only"]),
                "officers_in_charge": oics,
                "down_since": iso(since),
                "downtime_hours": round((now - since).total_seconds() / 3600, 1) if since else None,
                "last_status_change": iso(last_change.get(eid)),
                "upcoming_bookings": up.get("count", 0),
                "next_booking_at": iso(up.get("next_at")),
                "utilisation": utilisation,
                "booked_hours_30d": use["booked_hours"] if use else 0.0,
                "slot_hours_30d": use["slot_hours"] if use else 0.0,
            }
        )
        by_status[group] += 1
        by_category[r["category_id"]] += 1
        category_names[r["category_id"]] = r["category__name"]
        by_department[r["internal_department_id"]] += 1
        department_names[r["internal_department_id"]] = r["internal_department__name"]
        by_profile[r["profile_type"] or None] += 1
        if oics:
            for oic in oics:
                by_oic[oic["id"]] += 1
                oic_names[oic["id"]] = oic["name"]
        else:
            by_oic[None] += 1

    sort = str(params.get("sort") or "name").strip()
    key = SORTS.get(sort.lstrip("-"), SORTS["name"])
    rows.sort(key=key, reverse=sort.startswith("-"))
    offset, size = page_params(params)
    total = len(rows)
    payload = {
        **scope_payload(scope),
        "generated_at": now.isoformat(),
        "card": _equipment_status(scope),
        "summary": {
            "total": total,
            "by_status": [
                {"key": k, "label": label, "count": by_status.get(k, 0)}
                for k, label in STATUS_GROUPS.items()
                if k != "disposed" or by_status.get(k)
            ],
            "by_category": _breakdown(by_category, category_names, none_label="No category"),
            "by_department": _breakdown(by_department, department_names, none_label="No department"),
            "by_profile_type": _breakdown(
                by_profile, {k: str(v) for k, v in profile_labels.items()}, none_label="Not set"
            ),
            "by_oic": _breakdown(by_oic, oic_names, none_label="No OIC assigned"),
            "test_only": sum(1 for r in rows if r["test_only"]),
            "upcoming_bookings": sum(r["upcoming_bookings"] for r in rows),
            "utilisation": share(booked_total, slot_total),
            "utilisation_days": UTILISATION_DAYS,
        },
        "results": rows[offset : offset + size],
        **page_meta(total, offset, size),
    }
    if params.get("with_options"):
        payload["options"] = _options(scope)
    return payload
