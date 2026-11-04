"""Upkeep rules: who may see / change what, plan scheduling, uptime metrics and early-warning alerts.

Uptime is measured on calendar hours in the window. Downtime intervals come from:

* disruption events (``UNDER_MAINTENANCE`` = breakdown, ``SCHEDULED_MAINTENANCE`` = planned, ``OTHER`` = other
  downtime; ``OPERATOR_ABSENT`` is staff unavailability and is reported separately, not as equipment downtime);
* Procurement & Assets maintenance records that are not already tied to a disruption event (``BREAKDOWN`` =
  breakdown, every other kind = planned).

Overlapping intervals are merged, so the same outage recorded twice is counted once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import (
    AlertKind,
    AlertSeverity,
    MaintenancePlan,
    PlanCompletion,
    PlanKind,
    ReliabilityAlert,
    UpkeepSetting,
)

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 90
MAX_WINDOW_DAYS = 730
MAX_INTERVAL_DAYS = 1825
UPS_ALERT_LEVELS = ("attention", "urgent")


class UpkeepError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


# --------------------------------------------------------------------------- access
def visible_equipment_ids(user) -> set[int] | None:
    """None = every equipment (Main Admin). OIC / temporary OIC, Lab Operators and Department Admins see theirs."""
    from iic_booking.equipment.api_views import _get_equipment_ids_for_log_access
    from iic_booking.users.models.user_type import UserType

    if not user or not user.is_authenticated:
        return set()
    if user.user_type == UserType.ADMIN:
        return None
    if user.user_type not in (UserType.MANAGER, UserType.OPERATOR, UserType.DEPT_ADMIN):
        return set()
    return {int(x) for x in (_get_equipment_ids_for_log_access(user) or [])}


def managed_equipment_ids(user) -> set[int] | None:
    from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
    from iic_booking.users.models.user_type import UserType

    if not user or not user.is_authenticated:
        return set()
    if user.user_type == UserType.ADMIN:
        return None
    if user.user_type != UserType.MANAGER:
        return set()
    return {int(x) for x in get_equipment_ids_managed_by_oic(user.id)}


def _in(ids: set[int] | None, equipment_id: int) -> bool:
    return ids is None or int(equipment_id) in ids


def can_view(user, equipment_id: int) -> bool:
    return _in(visible_equipment_ids(user), equipment_id)


def can_manage(user, equipment_id: int) -> bool:
    """Plans, targets: the equipment's OIC (or temporary OIC) and the Main Admin."""
    return _in(managed_equipment_ids(user), equipment_id)


def can_record(user, plan: MaintenancePlan) -> bool:
    return can_view(user, plan.equipment_id) or (plan.responsible_id is not None and plan.responsible_id == user.pk)


def require(ok: bool, message: str = "You do not have access to this equipment's upkeep.") -> None:
    if not ok:
        raise UpkeepError(message, status=403, code="forbidden")


# --------------------------------------------------------------------------- settings
def setting_for(equipment) -> UpkeepSetting:
    return UpkeepSetting.objects.filter(equipment=equipment).first() or UpkeepSetting(equipment=equipment)


def _int(data, name, lo, hi, default=None):
    raw = data.get(name, default)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise UpkeepError(f"{name} must be a whole number.") from None
    if not lo <= v <= hi:
        raise UpkeepError(f"{name} must be between {lo} and {hi}.")
    return v


def save_setting(equipment, actor, data: dict) -> UpkeepSetting:
    require(can_manage(actor, equipment.pk), "Only the equipment's OIC or the Main Admin can change the uptime target.")
    row, _ = UpkeepSetting.objects.get_or_create(equipment=equipment)
    if "uptime_target_pct" in data:
        try:
            target = Decimal(str(data["uptime_target_pct"]))
        except (InvalidOperation, ValueError):
            raise UpkeepError("uptime_target_pct must be a number.") from None
        if not Decimal("0") <= target <= Decimal("100"):
            raise UpkeepError("uptime_target_pct must be between 0 and 100.")
        row.uptime_target_pct = target.quantize(Decimal("0.01"))
    if "breakdown_alert_count" in data:
        row.breakdown_alert_count = _int(data, "breakdown_alert_count", 0, 50)
    if "breakdown_window_days" in data:
        row.breakdown_window_days = _int(data, "breakdown_window_days", 7, 365)
    row.updated_by = actor
    row.save()
    return row


# --------------------------------------------------------------------------- plans
def plan_state(plan: MaintenancePlan, today: date | None = None) -> str:
    today = today or timezone.localdate()
    if not plan.is_active:
        return "CLOSED"
    if plan.next_due_on < today:
        return "OVERDUE"
    if plan.next_due_on <= today + timedelta(days=plan.lead_days):
        return "DUE"
    return "OK"


def _date(raw, name) -> date | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, date):
        return raw
    value = parse_date(str(raw)[:10])
    if value is None:
        raise UpkeepError(f"{name} must be a date (YYYY-MM-DD).")
    return value


def _responsible(raw, equipment):
    if raw in (None, ""):
        return None
    from iic_booking.users.models import User

    try:
        pk = int(raw)
    except (TypeError, ValueError):
        raise UpkeepError("responsible_id must be a user id.") from None
    user = User.objects.filter(pk=pk, is_active=True).first()
    if user is None:
        raise UpkeepError("Unknown responsible person.")
    if not can_view(user, equipment.pk):
        raise UpkeepError("The responsible person must be this equipment's OIC, a Lab Operator or a Department Admin.")
    return user


def _apply_plan(plan: MaintenancePlan, data: dict, *, creating: bool) -> None:
    if creating or "kind" in data:
        kind = data.get("kind") or PlanKind.PREVENTIVE
        if kind not in PlanKind.values:
            raise UpkeepError("Unknown plan kind.")
        plan.kind = kind
    if creating or "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            raise UpkeepError("A title is required.")
        plan.title = title[:200]
    if "instructions" in data:
        plan.instructions = str(data.get("instructions") or "")[:5000]
    if creating or "interval_days" in data:
        plan.interval_days = _int(data, "interval_days", 1, MAX_INTERVAL_DAYS)
    if "lead_days" in data:
        plan.lead_days = _int(data, "lead_days", 0, 180)
    if "responsible_id" in data:
        plan.responsible = _responsible(data.get("responsible_id"), plan.equipment)
    if "last_done_on" in data:
        plan.last_done_on = _date(data.get("last_done_on"), "last_done_on")
    nxt = _date(data.get("next_due_on"), "next_due_on") if "next_due_on" in data else None
    if nxt is not None:
        plan.next_due_on = nxt
    elif creating:
        base = plan.last_done_on or timezone.localdate()
        plan.next_due_on = base + timedelta(days=plan.interval_days)


def create_plan(equipment, actor, data: dict) -> MaintenancePlan:
    require(can_manage(actor, equipment.pk), "Only the equipment's OIC or the Main Admin can schedule maintenance.")
    plan = MaintenancePlan(equipment=equipment, created_by=actor)
    _apply_plan(plan, data, creating=True)
    plan.save()
    return plan


def update_plan(plan: MaintenancePlan, actor, data: dict) -> MaintenancePlan:
    require(can_manage(actor, plan.equipment_id), "Only the equipment's OIC or the Main Admin can change the schedule.")
    if not plan.is_active:
        raise UpkeepError("This plan is closed.")
    _apply_plan(plan, data, creating=False)
    plan.save()
    return plan


def close_plan(plan: MaintenancePlan, actor, reason: str) -> MaintenancePlan:
    require(can_manage(actor, plan.equipment_id), "Only the equipment's OIC or the Main Admin can close a plan.")
    reason = (reason or "").strip()
    if not reason:
        raise UpkeepError("A reason is required.", code="reason_required")
    plan.is_active, plan.closed_reason = False, reason[:2000]
    plan.save()
    ReliabilityAlert.objects.filter(equipment_id=plan.equipment_id, key__startswith=f"plan:{plan.pk}:",
                                    resolved_at__isnull=True).update(resolved_at=timezone.now())
    return plan


@transaction.atomic
def complete_plan(plan: MaintenancePlan, actor, data: dict) -> PlanCompletion:
    plan = MaintenancePlan.objects.select_for_update().get(pk=plan.pk)
    require(can_record(actor, plan), "Only the equipment's staff or the person responsible can record this.")
    if not plan.is_active:
        raise UpkeepError("This plan is closed.")
    today = timezone.localdate()
    done_on = _date(data.get("done_on"), "done_on") or today
    if done_on > today:
        raise UpkeepError("done_on cannot be in the future.")
    if plan.last_done_on and done_on < plan.last_done_on:
        raise UpkeepError("done_on is earlier than the last recorded completion.")
    next_due = done_on + timedelta(days=plan.interval_days)
    row = PlanCompletion.objects.create(
        plan=plan, done_on=done_on, done_by=actor, notes=str(data.get("notes") or "")[:5000],
        report_reference=str(data.get("report_reference") or "")[:120], next_due_on=next_due,
    )
    plan.last_done_on, plan.next_due_on = done_on, next_due
    plan.save(update_fields=["last_done_on", "next_due_on", "updated_at"])
    if plan_state(plan, today) == "OK":
        ReliabilityAlert.objects.filter(equipment_id=plan.equipment_id, key__startswith=f"plan:{plan.pk}:",
                                        resolved_at__isnull=True).update(resolved_at=timezone.now())
    return row


# --------------------------------------------------------------------------- uptime metrics
Interval = tuple[datetime, datetime]


def _merge(intervals: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _hours(intervals: list[Interval]) -> float:
    return sum((e - s).total_seconds() for s, e in intervals) / 3600.0


@dataclass
class _Raw:
    breakdown: list = field(default_factory=list)
    planned: list = field(default_factory=list)
    other: list = field(default_factory=list)
    staff: list = field(default_factory=list)
    causes: dict = field(default_factory=dict)


EVENT_LOOKBACK_DAYS = 400  # an outage longer than this is not looked for


def event_spans(ev, now) -> list[Interval]:
    """Whole-equipment events run until staff resume the equipment; slot events cover their slots only."""
    from iic_booking.equipment.disruption_models import DisruptionScope
    from iic_booking.equipment.disruption_service import _link_effective_end

    if ev.scope == DisruptionScope.EQUIPMENT:
        return [(ev.start_at, ev.ended_at or now)]
    out = []
    for link in ev.slot_links.all():
        e = _link_effective_end(link)
        if link.start_datetime and e and e > link.start_datetime:
            out.append((link.start_datetime, e))
    return out


def _events(equipment_ids, start, end, *, types=None):
    from iic_booking.equipment.disruption_models import DisruptionEvent

    qs = DisruptionEvent.objects.filter(start_at__lt=end, start_at__gte=start - timedelta(days=EVENT_LOOKBACK_DAYS))
    if equipment_ids is not None:
        qs = qs.filter(equipment_id__in=list(equipment_ids))
    if types:
        qs = qs.filter(disruption_type__in=types)
    return qs.prefetch_related("slot_links")


def _clip(spans, start, end) -> list[Interval]:
    return [(max(s, start), min(e, end)) for s, e in spans if min(e, end) > max(s, start)]


def _collect(equipment_ids, start, end, now) -> dict[int, _Raw]:
    from iic_booking.equipment.disruption_models import DisruptionType
    from iic_booking.equipment.disruption_service import reason_category_label

    raw: dict[int, _Raw] = {}
    for ev in _events(equipment_ids, start, end):
        spans = _clip(event_spans(ev, now), start, end)
        if not spans:
            continue
        r = raw.setdefault(ev.equipment_id, _Raw())
        bucket = {
            DisruptionType.UNDER_MAINTENANCE: r.breakdown,
            DisruptionType.SCHEDULED_MAINTENANCE: r.planned,
            DisruptionType.OPERATOR_ABSENT: r.staff,
        }.get(ev.disruption_type, r.other)
        bucket.extend(spans)
        if ev.disruption_type != DisruptionType.OPERATOR_ABSENT:
            label = reason_category_label(ev.disruption_type, ev.reason_category) or ev.get_disruption_type_display()
            c = r.causes.setdefault(label, [0, 0.0])
            c[0] += 1
            c[1] += _hours(_merge(spans))
    try:
        from iic_booking.procurement_management.models import MaintenanceRecord
    except Exception:  # pragma: no cover - module always installed here
        return raw
    recs = MaintenanceRecord.objects.filter(is_archived=False, disruption_event__isnull=True, downtime_start__isnull=False,
                                            downtime_start__lt=end).filter(Q(downtime_end__isnull=True) | Q(downtime_end__gt=start))
    if equipment_ids is not None:
        recs = recs.filter(equipment_id__in=list(equipment_ids))
    for rec in recs.only("equipment_id", "kind", "downtime_start", "downtime_end"):
        s, e = max(rec.downtime_start, start), min(rec.downtime_end or now, end)
        if e <= s:
            continue
        r = raw.setdefault(rec.equipment_id, _Raw())
        (r.breakdown if rec.kind == "BREAKDOWN" else r.planned).append((s, e))
        label = f"Maintenance record: {rec.get_kind_display()}"
        c = r.causes.setdefault(label, [0, 0.0])
        c[0] += 1
        c[1] += (e - s).total_seconds() / 3600.0
    return raw


def _summarise(r: _Raw | None, window_hours: float, target: Decimal) -> dict:
    r = r or _Raw()
    breakdowns = _merge(r.breakdown)
    down = _merge(r.breakdown + r.planned + r.other)
    planned_h = _hours(_merge(r.planned))
    down_h, breakdown_h = _hours(down), _hours(breakdowns)
    up_h = max(window_hours - down_h, 0.0)
    uptime = 100.0 * up_h / window_hours if window_hours else 100.0
    avail_base = max(window_hours - planned_h, 0.0)
    availability = 100.0 * max(avail_base - breakdown_h, 0.0) / avail_base if avail_base else 100.0
    n = len(breakdowns)
    return {
        "uptime_pct": round(uptime, 2),
        "availability_excl_planned_pct": round(availability, 2),
        "target_pct": float(target),
        "below_target": uptime < float(target),
        "downtime_hours": round(down_h, 1),
        "breakdown_hours": round(breakdown_h, 1),
        "planned_hours": round(planned_h, 1),
        "staff_unavailable_hours": round(_hours(_merge(r.staff)), 1),
        "breakdowns": n,
        "mttr_hours": round(breakdown_h / n, 1) if n else None,
        "mtbf_hours": round(up_h / n, 1) if n else None,
        "causes": sorted(
            ({"cause": k, "count": v[0], "hours": round(v[1], 1)} for k, v in r.causes.items()),
            key=lambda x: -x["hours"],
        ),
    }


def window(days) -> tuple[datetime, datetime, int]:
    try:
        days = int(days or DEFAULT_WINDOW_DAYS)
    except (TypeError, ValueError):
        raise UpkeepError("days must be a whole number.") from None
    if not 1 <= days <= MAX_WINDOW_DAYS:
        raise UpkeepError(f"days must be between 1 and {MAX_WINDOW_DAYS}.")
    end = timezone.now()
    return end - timedelta(days=days), end, days


def metrics(equipment, days=DEFAULT_WINDOW_DAYS) -> dict:
    start, end, days = window(days)
    raw = _collect({equipment.pk}, start, end, end)
    out = _summarise(raw.get(equipment.pk), (end - start).total_seconds() / 3600.0, setting_for(equipment).uptime_target_pct)
    return {"days": days, "from": start.isoformat(), "to": end.isoformat(), **out}


def metrics_many(equipment_list, days=DEFAULT_WINDOW_DAYS) -> dict[int, dict]:
    start, end, days = window(days)
    ids = {e.pk for e in equipment_list}
    raw = _collect(ids, start, end, end)
    targets = dict(UpkeepSetting.objects.filter(equipment_id__in=ids).values_list("equipment_id", "uptime_target_pct"))
    hours = (end - start).total_seconds() / 3600.0
    return {e.pk: _summarise(raw.get(e.pk), hours, targets.get(e.pk, Decimal("95.00"))) for e in equipment_list}


# --------------------------------------------------------------------------- alerts (daily scan)
def _staff(equipment, extra=()) -> list:
    from iic_booking.communication.in_app import equipment_oic_users
    from iic_booking.equipment.models import EquipmentOperator

    people = list(equipment_oic_users(equipment))
    people += [o.operator for o in EquipmentOperator.objects.filter(equipment=equipment).select_related("operator")]
    people += [p for p in extra if p is not None]
    seen, out = set(), []
    for p in people:
        if p is not None and p.pk not in seen and getattr(p, "is_active", True):
            seen.add(p.pk)
            out.append(p)
    return out


def _notify(alert: ReliabilityAlert, extra=()) -> None:
    title = f"{alert.get_kind_display()}: {alert.equipment.name}"
    link = f"/upkeep?equipment={alert.equipment_id}"
    users = _staff(alert.equipment, extra)
    if not users:
        return

    def send():
        try:
            from iic_booking.communication.in_app import notify_in_app

            notify_in_app(users, title=title, message=alert.message, link=link,
                          notification_type="warning" if alert.severity != AlertSeverity.INFO else "info",
                          event="upkeep.alert", extra={"equipment_id": alert.equipment_id, "kind": alert.kind})
        except Exception:
            logger.exception("upkeep in-app alert failed id=%s", alert.pk)
        try:
            from iic_booking.communication.models import CommunicationTemplate
            from iic_booking.communication.service import CommunicationService

            if CommunicationTemplate.objects.filter(code="upkeep_alert", is_active=True).exists():
                for u in users:
                    if getattr(u, "email", ""):
                        CommunicationService.send_email(u, template="upkeep_alert", template_context={
                            "title": title, "message": alert.message, "link": link,
                            "equipment_name": alert.equipment.name, "equipment_code": alert.equipment.code})
        except Exception:
            logger.exception("upkeep email alert failed id=%s", alert.pk)

    transaction.on_commit(send)


def _raise(equipment, kind, key, severity, message, seen: set, extra=()) -> bool:
    seen.add((equipment.pk, key))
    row = ReliabilityAlert.objects.filter(equipment=equipment, key=key, resolved_at__isnull=True).first()
    if row is not None:
        if row.message != message or row.severity != severity:
            row.message, row.severity = message, severity
        row.save()
        return False
    row = ReliabilityAlert.objects.create(equipment=equipment, kind=kind, key=key, severity=severity, message=message)
    _notify(row, extra)
    return True


def scan(today: date | None = None) -> dict:
    from iic_booking.equipment.disruption_models import DisruptionEvent, DisruptionType
    from iic_booking.equipment.models import Equipment

    today = today or timezone.localdate()
    now = timezone.now()
    seen: set = set()
    opened = 0
    # Plans due or overdue.
    for plan in MaintenancePlan.objects.filter(is_active=True).select_related("equipment", "responsible"):
        state = plan_state(plan, today)
        if state == "OVERDUE":
            days = (today - plan.next_due_on).days
            opened += _raise(plan.equipment, AlertKind.PLAN_OVERDUE, f"plan:{plan.pk}:overdue", AlertSeverity.CRITICAL,
                             f"{plan.title} ({plan.get_kind_display()}) was due on {plan.next_due_on:%d %b %Y}, "
                             f"{days} day(s) ago.", seen, [plan.responsible])
        elif state == "DUE":
            opened += _raise(plan.equipment, AlertKind.PLAN_DUE, f"plan:{plan.pk}:due", AlertSeverity.INFO,
                             f"{plan.title} ({plan.get_kind_display()}) is due on {plan.next_due_on:%d %b %Y}.",
                             seen, [plan.responsible])
    # Uptime and repeated breakdowns, for equipment with any downtime recently or an explicit setting.
    since = now - timedelta(days=365)
    ids = set(DisruptionEvent.objects.filter(Q(ended_at__isnull=True) | Q(ended_at__gt=since), start_at__gte=since - timedelta(days=EVENT_LOOKBACK_DAYS))
              .values_list("equipment_id", flat=True))
    ids |= set(DisruptionEvent.objects.filter(start_at__gte=since).values_list("equipment_id", flat=True))
    ids |= set(UpkeepSetting.objects.values_list("equipment_id", flat=True))
    equipment = list(Equipment.objects.filter(pk__in=ids))
    settings_by_id = {s.equipment_id: s for s in UpkeepSetting.objects.filter(equipment_id__in=ids)}
    uptime = metrics_many(equipment, DEFAULT_WINDOW_DAYS)
    for eq in equipment:
        cfg = settings_by_id.get(eq.pk) or UpkeepSetting(equipment=eq)
        m = uptime[eq.pk]
        if m["below_target"]:
            opened += _raise(eq, AlertKind.UPTIME_BELOW_TARGET, "uptime", AlertSeverity.WARNING,
                             f"Uptime over the last {DEFAULT_WINDOW_DAYS} days is {m['uptime_pct']}%, below the "
                             f"{m['target_pct']}% target ({m['downtime_hours']} h down).", seen)
        if cfg.breakdown_alert_count:
            start = now - timedelta(days=cfg.breakdown_window_days)
            n = len(_merge([
                span
                for ev in _events({eq.pk}, start, now, types=[DisruptionType.UNDER_MAINTENANCE])
                for span in _clip(event_spans(ev, now), start, now)
            ]))
            if n >= cfg.breakdown_alert_count:
                opened += _raise(eq, AlertKind.REPEATED_BREAKDOWNS, "breakdowns", AlertSeverity.WARNING,
                                 f"{n} breakdowns in the last {cfg.breakdown_window_days} days: consider a service "
                                 f"visit or root-cause check before the next failure.", seen)
    # UPS health from the power module.
    try:
        from iic_booking.power.models import UpsDevice
    except Exception:  # pragma: no cover
        UpsDevice = None
    if UpsDevice is not None:
        for dev in UpsDevice.objects.filter(enabled=True).prefetch_related("equipment"):
            latest = dev.assessments.order_by("-created_at").first()
            if latest is None or latest.level not in UPS_ALERT_LEVELS:
                continue
            for eq in dev.equipment.all():
                opened += _raise(eq, AlertKind.UPS_HEALTH, f"ups:{dev.pk}",
                                 AlertSeverity.CRITICAL if latest.level == "urgent" else AlertSeverity.WARNING,
                                 f"UPS {dev.name} powering this equipment: {latest.get_level_display()}. "
                                 f"{(latest.summary or '').strip()[:400]}".strip(), seen)
    # Conditions no longer present are resolved.
    resolved = 0
    for row in ReliabilityAlert.objects.filter(resolved_at__isnull=True).only("pk", "equipment_id", "key"):
        if (row.equipment_id, row.key) not in seen:
            ReliabilityAlert.objects.filter(pk=row.pk).update(resolved_at=now)
            resolved += 1
    return {"opened": opened, "resolved": resolved, "open": ReliabilityAlert.objects.filter(resolved_at__isnull=True).count()}


def acknowledge(alert: ReliabilityAlert, actor, note: str = "") -> ReliabilityAlert:
    require(can_view(actor, alert.equipment_id))
    alert.acknowledged_by, alert.acknowledged_at = actor, timezone.now()
    alert.acknowledge_note = (note or "")[:2000]
    alert.save(update_fields=["acknowledged_by", "acknowledged_at", "acknowledge_note", "last_seen_at"])
    return alert
