"""
Recurring slot block rules ("Repeat block" on the Change slot status page).

A rule repeats one operation of the page's Select operation list on chosen weekdays and slot start times (local time)
within a date range: on existing slots when the rule is created, and on new slots as they are generated later.

- Status rules (Other Reasons, Under Maintenance, Scheduled Maintenance, Operator Absent, Not Available, Reserved for
  External) give that status to slots that are AVAILABLE.
- The Available rule opens slots that are Not Available (e.g. Saturdays or holidays), and makes the generator create
  those slots for everyone; it never opens anything while the equipment is under repair.
- The non-home reservation rules (Department-Based Slot Reservation on) set or clear "Reserved for non-home
  department" on slots in any status.

Booked and already-started slots are never touched, and nothing here cancels or refunds a booking. One-off actions on
bookings (Create booking, Booking Not Utilized, Reschedule, Bulk email) cannot repeat.

Each slot a rule changes is linked to it (RecurringSlotBlockRuleSlot), so removing the rule reverts only its own
future, still-unbooked slots. A slot also covered by another active rule of the same operation stays as it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from iic_booking.communication.email_branding import strftime_slot_end
from iic_booking.equipment.dept_admin_actions import record_staff_action
from iic_booking.equipment.models import (
    DailySlot,
    Equipment,
    Holiday,
    RecurringSlotBlockRule,
    RecurringSlotBlockRuleSlot,
    SlotMaster,
    SlotStatus,
    format_slot_close_time,
    slot_master_duration_minutes,
)
from iic_booking.equipment.slot_utils import SlotGenerator

logger = logging.getLogger(__name__)

WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_RANGE_DAYS = 400
LIST_LIMIT = 500

SKIP_REASONS = {
    SlotStatus.AVAILABLE: "Already available",
    SlotStatus.BLOCKED: "Already blocked (Other Reasons)",
    SlotStatus.UNDER_MAINTENANCE: "Under maintenance",
    SlotStatus.OPERATOR_ABSENT: "Operator absent",
    SlotStatus.NOT_AVAILABLE: "Not available",
    SlotStatus.SCHEDULED_MAINTENANCE: "Scheduled maintenance",
    SlotStatus.RESERVED_EXTERNAL: "Reserved (External)",
    SlotStatus.BOOKING_NOT_UTILIZED: "Booking not utilized",
    SlotStatus.BOOKED: "Booked",
}
SHARED_REASON = "Already blocked by another repeat rule"
IN_REPAIR_REASON = "Equipment is under repair"

# Rule values for the non-home reservation (a slot flag, not a status). DailySlot / rule slot_status: max 20 chars.
RESERVE_NON_HOME = "HOME_DEPARTMENT_ONLY"
CLEAR_NON_HOME = "CLEAR_HOME_DEPT"
FLAG_RULES = {RESERVE_NON_HOME: True, CLEAR_NON_HOME: False}
FLAG_SKIP_REASONS = {
    RESERVE_NON_HOME: "Already reserved for non-home department",
    CLEAR_NON_HOME: "Not reserved for non-home department",
}

# Operations a repeat rule may repeat. Order = the Select operation dropdown order.
RULE_STATUSES = (
    SlotStatus.BLOCKED,
    SlotStatus.UNDER_MAINTENANCE,
    SlotStatus.SCHEDULED_MAINTENANCE,
    SlotStatus.OPERATOR_ABSENT,
    SlotStatus.AVAILABLE,
    SlotStatus.NOT_AVAILABLE,
    SlotStatus.RESERVED_EXTERNAL,
    RESERVE_NON_HOME,
    CLEAR_NON_HOME,
)
RULE_STATUS_LABELS = {
    SlotStatus.BLOCKED: "Other Reasons",
    SlotStatus.UNDER_MAINTENANCE: "Under Maintenance",
    SlotStatus.SCHEDULED_MAINTENANCE: "Scheduled Maintenance",
    SlotStatus.OPERATOR_ABSENT: "Operator Absent",
    SlotStatus.AVAILABLE: "Available",
    SlotStatus.NOT_AVAILABLE: "Not Available",
    SlotStatus.RESERVED_EXTERNAL: "Reserved for External",
    RESERVE_NON_HOME: "Reserved for non-home department",
    CLEAR_NON_HOME: "Clear non-home reservation (home dept)",
}


def department_reservation_on(equipment) -> bool:
    return bool(
        equipment is not None
        and getattr(equipment, "department_slot_reservation_enabled", False)
        and getattr(equipment, "internal_department_id", None)
    )


def rule_status_options(equipment=None) -> list[dict]:
    """Dropdown options; the two non-home reservation rules only where Department-Based Slot Reservation is on."""
    allowed = [s for s in RULE_STATUSES if s not in FLAG_RULES or department_reservation_on(equipment)]
    return [{"value": s, "label": RULE_STATUS_LABELS[s]} for s in allowed]


def _rule_status(rule) -> str:
    value = str(getattr(rule, "slot_status", "") or SlotStatus.BLOCKED)
    return value if value in RULE_STATUSES else SlotStatus.BLOCKED


def is_flag_rule(slot_status: str) -> bool:
    return slot_status in FLAG_RULES


def _from_status(slot_status: str) -> str:
    """Status a status rule changes: Available opens Not Available slots; every other status takes Available ones."""
    return SlotStatus.NOT_AVAILABLE if slot_status == SlotStatus.AVAILABLE else SlotStatus.AVAILABLE


def _is_applied(slot, slot_status: str) -> bool:
    """The slot already has what the rule gives."""
    if is_flag_rule(slot_status):
        return bool(getattr(slot, "home_department_only", False)) == FLAG_RULES[slot_status]
    return slot.status == slot_status


def _equipment_can_open(equipment) -> bool:
    return SlotGenerator._get_initial_slot_status_for_equipment(equipment) == SlotStatus.AVAILABLE


def _stored_label(slot_status: str, label: str) -> str | None:
    """DailySlot.blocked_label: only Other Reasons slots carry the rule's label."""
    return _label_value(label) if slot_status == SlotStatus.BLOCKED else None

Source = RecurringSlotBlockRuleSlot.Source


class RuleInputError(Exception):
    def __init__(self, message: str, field: str = "", code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.field = field
        self.code = code


def _hhmm(value: time) -> str:
    return value.strftime("%H:%M")


def _local_start_hhmm(slot: DailySlot) -> str:
    return timezone.localtime(slot.start_datetime).strftime("%H:%M")


def _label_value(label: str) -> str | None:
    return (label or "").strip() or None


def equipment_slot_times(equipment: Equipment) -> list[dict]:
    """Distinct local start times of the equipment's active Slot Masters, earliest first."""
    rows: dict[str, dict] = {}
    for master in SlotMaster.objects.filter(equipment=equipment, is_active=True).order_by("open_time", "slot_number"):
        key = _hhmm(master.open_time)
        rows.setdefault(
            key,
            {
                "time": key,
                "end_time": format_slot_close_time(master.close_time),
                "duration_minutes": slot_master_duration_minutes(master.open_time, master.close_time),
                "name": master.slot_name or f"Slot {master.slot_number}",
            },
        )
    return sorted(rows.values(), key=lambda r: r["time"])


def _parse_date(raw, field_name: str) -> date | None:
    if raw in (None, ""):
        return None
    try:
        return datetime.strptime(str(raw).strip()[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise RuleInputError("Use dates in YYYY-MM-DD format.", field_name, "invalid_date") from exc


def clean_rule_input(equipment: Equipment, data) -> dict:
    """Validate a preview/create payload; raises RuleInputError with the offending field."""
    data = data or {}
    today = timezone.localdate()

    raw_days = data.get("weekdays")
    if not isinstance(raw_days, list) or not raw_days:
        raise RuleInputError("Pick at least one weekday.", "weekdays", "weekdays_required")
    weekdays: set[int] = set()
    for raw in raw_days:
        try:
            day = int(raw)
        except (TypeError, ValueError):
            day = -1
        if not 0 <= day <= 6:
            raise RuleInputError("Weekdays must be 0 (Monday) to 6 (Sunday).", "weekdays", "invalid_weekday")
        weekdays.add(day)

    raw_times = data.get("slot_times")
    if not isinstance(raw_times, list) or not raw_times:
        raise RuleInputError("Pick at least one slot time.", "slot_times", "slot_times_required")
    allowed = {row["time"] for row in equipment_slot_times(equipment)}
    slot_times: set[str] = set()
    for raw in raw_times:
        value = str(raw or "").strip()[:5]
        if value not in allowed:
            raise RuleInputError(
                f"{value or raw} is not a slot time of this equipment.", "slot_times", "unknown_slot_time"
            )
        slot_times.add(value)

    start = _parse_date(data.get("start_date"), "start_date") or today
    end = _parse_date(data.get("end_date"), "end_date")
    if end is None:
        raise RuleInputError("Pick an end date.", "end_date", "end_date_required")
    if start < today:
        raise RuleInputError("The start date cannot be in the past.", "start_date", "start_in_past")
    if end < start:
        raise RuleInputError("The end date must be on or after the start date.", "end_date", "end_before_start")
    if (end - start).days > MAX_RANGE_DAYS:
        raise RuleInputError("Pick a range of at most 13 months.", "end_date", "range_too_long")

    label = str(data.get("label") or "").strip()
    if len(label) > 255:
        raise RuleInputError("The label can be at most 255 characters.", "label", "label_too_long")

    slot_status = str(data.get("slot_status") or SlotStatus.BLOCKED).strip().upper()
    options = [o["value"] for o in rule_status_options(equipment)]
    if slot_status in FLAG_RULES and slot_status not in options:
        raise RuleInputError(
            "Department-Based Slot Reservation is switched off for this equipment (or it has no home department). "
            "The Main Administrator can switch it on in the equipment settings.",
            "slot_status",
            "department_slot_reservation_off",
        )
    if slot_status not in options:
        raise RuleInputError(
            "Choose an operation: " + ", ".join(RULE_STATUS_LABELS[s] for s in options) + ".",
            "slot_status",
            "invalid_slot_status",
        )

    return {
        "weekdays": sorted(weekdays),
        "slot_times": sorted(slot_times),
        "start_date": start,
        "end_date": end,
        "label": label,
        "slot_status": slot_status,
    }


def _candidate_slots(equipment: Equipment, cleaned: dict, *, lock: bool = False) -> list[DailySlot]:
    """Existing, not-yet-started slots on the rule's weekdays and local start times."""
    qs = DailySlot.objects.filter(
        slot_master__equipment=equipment,
        date__gte=cleaned["start_date"],
        date__lte=cleaned["end_date"],
        date__iso_week_day__in=[d + 1 for d in cleaned["weekdays"]],
        start_datetime__gt=timezone.now(),
    ).order_by("date", "start_datetime")
    if lock:
        qs = qs.select_for_update(of=("self",))
    qs = qs.select_related("booking", "booking__user")
    times = set(cleaned["slot_times"])
    return [s for s in qs if _local_start_hhmm(s) in times]


def _slot_row(slot: DailySlot) -> dict:
    return {
        "slot_id": slot.pk,
        "date": slot.date.isoformat(),
        "weekday": WEEKDAY_LABELS[slot.date.weekday()],
        "start_time": timezone.localtime(slot.start_datetime).strftime("%H:%M"),
        "end_time": strftime_slot_end(timezone.localtime(slot.end_datetime), "%H:%M"),
    }


def _booked_row(slot: DailySlot) -> dict:
    row = _slot_row(slot)
    booking = slot.booking
    user = getattr(booking, "user", None)
    row.update(
        {
            "booking_id": getattr(booking, "pk", None),
            "booking_reference": (getattr(booking, "virtual_booking_id", "") or str(getattr(booking, "pk", "") or "")),
            "booking_status": getattr(booking, "status", "") or "",
            "user_name": (getattr(user, "name", "") or getattr(user, "email", "") or "") if user else "",
        }
    )
    return row


def _other_row(slot: DailySlot, reason: str) -> dict:
    row = _slot_row(slot)
    row.update({"status": slot.status, "reason": reason, "blocked_label": slot.blocked_label or ""})
    return row


@dataclass
class _Plan:
    to_block: list[DailySlot] = field(default_factory=list)
    booked: list[DailySlot] = field(default_factory=list)
    shared: list[DailySlot] = field(default_factory=list)
    other: list[tuple[DailySlot, str]] = field(default_factory=list)


def _classify(
    slots: list[DailySlot],
    *,
    slot_status: str = SlotStatus.BLOCKED,
    exclude_rule_id: int | None = None,
    can_open: bool = True,
) -> _Plan:
    plan = _Plan()
    applied_ids = [s.pk for s in slots if not s.booking_id and _is_applied(s, slot_status)]
    shared_ids: set[int] = set()
    if applied_ids:
        links = RecurringSlotBlockRuleSlot.objects.filter(
            daily_slot_id__in=applied_ids, rule__is_active=True, rule__slot_status=slot_status
        )
        if exclude_rule_id:
            links = links.exclude(rule_id=exclude_rule_id)
        shared_ids = set(links.values_list("daily_slot_id", flat=True))
    flag = is_flag_rule(slot_status)
    source = None if flag else _from_status(slot_status)
    for slot in slots:
        if slot.booking_id:
            plan.booked.append(slot)
        elif flag and not _is_applied(slot, slot_status):
            plan.to_block.append(slot)
        elif not flag and slot.status == source:
            if slot_status == SlotStatus.AVAILABLE and not can_open:
                plan.other.append((slot, IN_REPAIR_REASON))
            else:
                plan.to_block.append(slot)
        elif slot.pk in shared_ids:
            plan.shared.append(slot)
            plan.other.append((slot, SHARED_REASON))
        elif flag:
            plan.other.append((slot, FLAG_SKIP_REASONS[slot_status]))
        else:
            plan.other.append((slot, SKIP_REASONS.get(slot.status, str(slot.status))))
    return plan


def _future_slot_count(equipment: Equipment, cleaned: dict) -> int:
    """Matching slots not generated yet that the rule will change when they are generated."""
    slot_status = cleaned.get("slot_status", SlotStatus.BLOCKED)
    opening = slot_status == SlotStatus.AVAILABLE
    if not is_flag_rule(slot_status) and not _equipment_can_open(equipment):
        return 0
    times = set(cleaned["slot_times"])
    masters = [
        m for m in SlotMaster.objects.filter(equipment=equipment, is_active=True) if _hhmm(m.open_time) in times
    ]
    if not masters:
        return 0
    start, end = cleaned["start_date"], cleaned["end_date"]
    existing = set(
        DailySlot.objects.filter(slot_master__in=masters, date__gte=start, date__lte=end).values_list(
            "slot_master_id", "date"
        )
    )
    holidays = Holiday.get_holidays_in_range(start, end)
    weekdays = set(cleaned["weekdays"])
    now = timezone.now()
    count = 0
    current = start
    while current <= end:
        # New slots on weekends/holidays are generated Not Available: only the Available rule changes them.
        if current.weekday() in weekdays and (current in holidays) == opening:
            for master in masters:
                if (master.pk, current) in existing:
                    continue
                slot_start, _ = SlotGenerator._aware_slot_datetimes(
                    current, master.open_time, master.close_time, equipment.operating_day_start
                )
                if slot_start > now:
                    count += 1
        current += timedelta(days=1)
    return count


def _slots_exist_until(equipment: Equipment, cleaned: dict) -> str | None:
    last = (
        DailySlot.objects.filter(
            slot_master__equipment=equipment,
            date__gte=cleaned["start_date"],
            date__lte=cleaned["end_date"],
        )
        .order_by("-date")
        .values_list("date", flat=True)
        .first()
    )
    return last.isoformat() if last else None


def _plan_summary(plan: _Plan, equipment: Equipment, cleaned: dict) -> dict:
    booked = [_booked_row(s) for s in plan.booked]
    other = [_other_row(s, reason) for s, reason in plan.other]
    return {
        "matched_count": len(plan.to_block) + len(plan.booked) + len(plan.other),
        "to_block_count": len(plan.to_block),
        "skipped_booked_count": len(booked),
        "skipped_booked": booked[:LIST_LIMIT],
        "skipped_other_count": len(other),
        "skipped_other": other[:LIST_LIMIT],
        "already_blocked_by_rule_count": len(plan.shared),
        "future_slots_count": _future_slot_count(equipment, cleaned),
        "slots_exist_until": _slots_exist_until(equipment, cleaned),
        "list_limit": LIST_LIMIT,
    }


def preview_rule(equipment: Equipment, cleaned: dict) -> dict:
    """Dry run of create_rule: what would be changed and skipped. Writes nothing."""
    plan = _classify(
        _candidate_slots(equipment, cleaned),
        slot_status=cleaned.get("slot_status", SlotStatus.BLOCKED),
        can_open=_equipment_can_open(equipment),
    )
    return _plan_summary(plan, equipment, cleaned)


def _record_rule_disruptions(equipment: Equipment, slot_ids: list[int], slot_status: str, label: str, actor) -> None:
    """Under Maintenance / Operator Absent / Scheduled Maintenance slots join (or open) a disruption event."""
    from .disruption_service import DisruptionInput, auto_recorded_slot_statuses, clean_text, record_slot_status_change

    if not slot_ids or slot_status not in auto_recorded_slot_statuses():
        return
    slots = list(DailySlot.objects.filter(pk__in=slot_ids))
    for slot in slots:
        slot._disruption_old_status = SlotStatus.AVAILABLE
    data = DisruptionInput(user=actor if getattr(actor, "pk", None) else None, source="CHANGE_SLOT_STATUS")
    if label:
        data.label = clean_text(label, 255)
        if not data.reason:
            data.reason = clean_text(label)
    record_slot_status_change(equipment, slots, slot_status, data, log_change=False)


def create_rule(equipment: Equipment, cleaned: dict, actor) -> tuple[RecurringSlotBlockRule, dict]:
    """Save the rule and apply it to its matching existing slots now, in one transaction."""
    slot_status = cleaned.get("slot_status", SlotStatus.BLOCKED)
    with transaction.atomic():
        plan = _classify(
            _candidate_slots(equipment, cleaned, lock=True),
            slot_status=slot_status,
            can_open=_equipment_can_open(equipment),
        )
        rule = RecurringSlotBlockRule.objects.create(
            equipment=equipment,
            weekdays=cleaned["weekdays"],
            slot_times=cleaned["slot_times"],
            start_date=cleaned["start_date"],
            end_date=cleaned["end_date"],
            label=cleaned["label"],
            slot_status=slot_status,
            created_by=actor if getattr(actor, "pk", None) else None,
        )
        block_ids = [s.pk for s in plan.to_block]
        blocked = 0
        if block_ids:
            qs = DailySlot.objects.filter(pk__in=block_ids, booking__isnull=True)
            if is_flag_rule(slot_status):
                blocked = qs.update(home_department_only=FLAG_RULES[slot_status])
            else:
                blocked = qs.filter(status=_from_status(slot_status)).update(
                    status=slot_status, blocked_label=_stored_label(slot_status, cleaned["label"])
                )
        links = [RecurringSlotBlockRuleSlot(rule=rule, daily_slot_id=pk, source=Source.CREATED) for pk in block_ids]
        links += [RecurringSlotBlockRuleSlot(rule=rule, daily_slot_id=s.pk, source=Source.SHARED) for s in plan.shared]
        if links:
            RecurringSlotBlockRuleSlot.objects.bulk_create(links, ignore_conflicts=True)
        summary = _plan_summary(plan, equipment, cleaned)
        summary["blocked_count"] = blocked
        rule.summary = {k: v for k, v in summary.items() if k != "slots_exist_until"}
        rule.save(update_fields=["summary"])
    if blocked and not is_flag_rule(slot_status):
        _record_rule_disruptions(equipment, block_ids, slot_status, cleaned["label"], actor)

    record_staff_action(
        actor,
        "recurring_slot_block_rule_created",
        equipment_id=equipment.pk,
        rule_id=rule.pk,
        weekdays=cleaned["weekdays"],
        slot_times=cleaned["slot_times"],
        start_date=str(cleaned["start_date"]),
        end_date=str(cleaned["end_date"]),
        slot_status=slot_status,
        blocked=blocked,
        skipped_booked=summary["skipped_booked_count"],
        skipped_other=summary["skipped_other_count"],
    )
    return rule, summary


@dataclass
class _RemovalPlan:
    unblock: list[DailySlot] = field(default_factory=list)
    kept: list[tuple[DailySlot, RecurringSlotBlockRule]] = field(default_factory=list)
    unchanged: list[DailySlot] = field(default_factory=list)


def _removal_plan(rule: RecurringSlotBlockRule, *, lock: bool = False) -> _RemovalPlan:
    slot_ids = list(
        rule.slot_links.filter(daily_slot__start_datetime__gt=timezone.now()).values_list("daily_slot_id", flat=True)
    )
    plan = _RemovalPlan()
    if not slot_ids:
        return plan
    slots_qs = DailySlot.objects.filter(pk__in=slot_ids).order_by("date", "start_datetime")
    if lock:
        slots_qs = slots_qs.select_for_update()
    rule_status = _rule_status(rule)
    other_rule_by_slot: dict[int, RecurringSlotBlockRule] = {}
    for link in (
        RecurringSlotBlockRuleSlot.objects.filter(
            daily_slot_id__in=slot_ids, rule__is_active=True, rule__slot_status=rule_status
        )
        .exclude(rule_id=rule.pk)
        .select_related("rule")
        .order_by("rule__created_at", "rule_id")
    ):
        other_rule_by_slot.setdefault(link.daily_slot_id, link.rule)
    own_label = _stored_label(rule_status, rule.label) or ""
    flag = is_flag_rule(rule_status)
    for slot in slots_qs:
        if slot.booking_id or not _is_applied(slot, rule_status):
            plan.unchanged.append(slot)
        elif slot.pk in other_rule_by_slot:
            plan.kept.append((slot, other_rule_by_slot[slot.pk]))
        elif flag or (slot.blocked_label or "") == own_label:
            plan.unblock.append(slot)
        else:
            # Label changed since the rule blocked it: someone re-blocked it by hand.
            plan.unchanged.append(slot)
    return plan


def removal_preview(rule: RecurringSlotBlockRule) -> dict:
    plan = _removal_plan(rule)
    return {
        "will_unblock_count": len(plan.unblock),
        "kept_by_other_rule_count": len(plan.kept),
        "unchanged_count": len(plan.unchanged),
    }


def _restore_status(equipment: Equipment, rule_status: str) -> str:
    if rule_status == SlotStatus.AVAILABLE:
        return SlotStatus.NOT_AVAILABLE
    return SlotGenerator._get_initial_slot_status_for_equipment(equipment)


def remove_rule(rule: RecurringSlotBlockRule, actor) -> dict:
    """Deactivate the rule and revert its own future, unbooked slots no other active rule of its kind covers."""
    equipment = rule.equipment
    with transaction.atomic():
        rule = RecurringSlotBlockRule.objects.select_for_update().get(pk=rule.pk)
        if not rule.is_active:
            raise RuleInputError("This repeat block was already removed.", code="already_removed")
        plan = _removal_plan(rule, lock=True)
        rule_status = _rule_status(rule)
        flag = is_flag_rule(rule_status)
        restore_status = None if flag else _restore_status(equipment, rule_status)
        unblocked = 0
        if plan.unblock:
            ids = [s.pk for s in plan.unblock]
            if flag:
                target = FLAG_RULES[rule_status]
                unblocked = DailySlot.objects.filter(
                    pk__in=ids, home_department_only=target, booking__isnull=True
                ).update(home_department_only=not target)
            else:
                unblocked = DailySlot.objects.filter(pk__in=ids, status=rule_status, booking__isnull=True).update(
                    status=restore_status, blocked_label=None
                )
                if unblocked:
                    _record_restored_disruptions(equipment, ids, restore_status, actor, old_status=rule_status)
        if rule_status == SlotStatus.BLOCKED:
            own_label = _stored_label(rule_status, rule.label) or ""
            handover: dict[int, tuple[RecurringSlotBlockRule, list[int]]] = {}
            for slot, other in plan.kept:
                if (slot.blocked_label or "") == own_label:
                    handover.setdefault(other.pk, (other, []))[1].append(slot.pk)
            for other, ids in handover.values():
                DailySlot.objects.filter(pk__in=ids, status=rule_status).update(
                    blocked_label=_stored_label(rule_status, other.label)
                )
        result = {
            "unblocked_count": unblocked,
            "kept_by_other_rule_count": len(plan.kept),
            "unchanged_count": len(plan.unchanged),
            "restored_status": restore_status,
        }
        if flag:
            result["restored_home_department_only"] = not FLAG_RULES[rule_status]
        rule.is_active = False
        rule.removed_by = actor if getattr(actor, "pk", None) else None
        rule.removed_at = timezone.now()
        rule.removal_summary = result
        rule.save(update_fields=["is_active", "removed_by", "removed_at", "removal_summary"])

    record_staff_action(
        actor,
        "recurring_slot_block_rule_removed",
        equipment_id=equipment.pk,
        rule_id=rule.pk,
        **result,
    )
    return result


def serialize_rule(rule: RecurringSlotBlockRule, *, with_removal_preview: bool = True) -> dict:
    def _name(user):
        return (getattr(user, "name", "") or getattr(user, "email", "") or "") if user else ""

    rule_status = _rule_status(rule)
    if is_flag_rule(rule_status):
        applied = rule.slot_links.filter(daily_slot__home_department_only=FLAG_RULES[rule_status])
    else:
        applied = rule.slot_links.filter(daily_slot__status=rule_status)
    data = {
        "id": rule.pk,
        "equipment_id": rule.equipment_id,
        "weekdays": list(rule.weekdays or []),
        "weekday_labels": [WEEKDAY_LABELS[d] for d in (rule.weekdays or []) if 0 <= int(d) <= 6],
        "slot_times": list(rule.slot_times or []),
        "start_date": rule.start_date.isoformat(),
        "end_date": rule.end_date.isoformat(),
        "label": rule.label or "",
        "slot_status": rule_status,
        "slot_status_display": RULE_STATUS_LABELS[rule_status],
        "is_active": rule.is_active,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "created_by_name": _name(rule.created_by),
        "removed_at": rule.removed_at.isoformat() if rule.removed_at else None,
        "removed_by_name": _name(rule.removed_by),
        "summary": rule.summary or {},
        "removal_summary": rule.removal_summary or {},
        "blocked_now_count": applied.exclude(source=Source.SHARED).count(),
        "generated_count": rule.slot_links.filter(source=Source.GENERATED).count(),
    }
    if with_removal_preview and rule.is_active:
        data["removal_preview"] = removal_preview(rule)
    return data


# --- Slot generation hook ---------------------------------------------------------------------


def _rule_covers(rule, slot_date: date, hhmm: str) -> bool:
    return (
        rule.start_date <= slot_date <= rule.end_date
        and slot_date.weekday() in (rule.weekdays or [])
        and hhmm in (rule.slot_times or [])
    )


def opening_rules(equipment: Equipment, start: date, end: date) -> list[RecurringSlotBlockRule]:
    """Active Available rules overlapping [start, end] (none while the equipment cannot be opened)."""
    if not _equipment_can_open(equipment):
        return []
    return list(
        RecurringSlotBlockRule.objects.filter(
            equipment=equipment,
            is_active=True,
            slot_status=SlotStatus.AVAILABLE,
            start_date__lte=end,
            end_date__gte=start,
        )
    )


def masters_opened_on(rules: list, masters: list, slot_date: date) -> list:
    """Slot Masters an Available rule opens on a weekend/holiday date (generated for everyone, not only staff)."""
    if not rules:
        return []
    return [m for m in masters if any(_rule_covers(r, slot_date, _hhmm(m.open_time)) for r in rules)]


def _apply_rules_to_new_rows(equipment: Equipment, rows: list[DailySlot]) -> dict[tuple[int, date], list[int]]:
    """Apply active rules to unsaved rows; returns (slot_master_id, date) -> ids of the rules that changed the row.

    Order: the non-home reservation mark, then Available (opens Not Available rows), then the status rules (take
    Available rows). The first rule of each kind wins; later rules of the same operation share the slot."""
    dates = [r.date for r in rows]
    rules = list(
        RecurringSlotBlockRule.objects.filter(
            equipment=equipment,
            is_active=True,
            start_date__lte=max(dates),
            end_date__gte=min(dates),
        ).order_by("created_at", "pk")
    )
    if not rules:
        return {}
    can_open = _equipment_can_open(equipment)
    now = timezone.now()
    matches: dict[tuple[int, date], list[int]] = {}
    for row in rows:
        if row.start_datetime <= now:
            continue
        hhmm = _local_start_hhmm(row)
        hits = [r for r in rules if _rule_covers(r, row.date, hhmm)]
        if not hits:
            continue
        linked: list[int] = []

        flag_hits = [r for r in hits if is_flag_rule(_rule_status(r))]
        if flag_hits:
            value = FLAG_RULES[_rule_status(flag_hits[0])]
            if bool(row.home_department_only) != value:
                row.home_department_only = value
                linked += [r.pk for r in flag_hits if FLAG_RULES[_rule_status(r)] == value]

        open_hits = [r for r in hits if _rule_status(r) == SlotStatus.AVAILABLE]
        if open_hits and can_open and row.status == SlotStatus.NOT_AVAILABLE:
            row.status = SlotStatus.AVAILABLE
            row.blocked_label = None
            linked += [r.pk for r in open_hits]

        status_hits = [
            r for r in hits if not is_flag_rule(_rule_status(r)) and _rule_status(r) != SlotStatus.AVAILABLE
        ]
        if status_hits and row.status == SlotStatus.AVAILABLE:
            first_status = _rule_status(status_hits[0])
            row.status = first_status
            row.blocked_label = _stored_label(first_status, status_hits[0].label)
            linked += [r.pk for r in status_hits if _rule_status(r) == first_status]

        if linked:
            matches[(row.slot_master_id, row.date)] = linked
    return matches


def _link_generated_rows(
    matches: dict[tuple[int, date], list[int]],
    expected: dict[tuple[int, date], tuple[str, str | None, bool]],
):
    """Link rows this call created (an existing row from a concurrent load differs and is not linked)."""
    master_ids = {k[0] for k in matches}
    dates = {k[1] for k in matches}
    links = []
    for pk, master_id, slot_date, slot_status, blocked_label, reserved in DailySlot.objects.filter(
        slot_master_id__in=master_ids, date__in=dates, booking__isnull=True
    ).values_list("pk", "slot_master_id", "date", "status", "blocked_label", "home_department_only"):
        key = (master_id, slot_date)
        if key not in matches or expected[key] != (slot_status, blocked_label or None, bool(reserved)):
            continue
        links.extend(
            RecurringSlotBlockRuleSlot(rule_id=rule_id, daily_slot_id=pk, source=Source.GENERATED)
            for rule_id in matches[key]
        )
    if links:
        RecurringSlotBlockRuleSlot.objects.bulk_create(links, ignore_conflicts=True)


def bulk_create_daily_slots(equipment: Equipment, rows: list[DailySlot]) -> list[DailySlot]:
    """bulk_create new DailySlot rows (ignore_conflicts), applying the active repeat rules that cover them."""
    if not rows:
        return []
    matches = _apply_rules_to_new_rows(equipment, rows)
    if not matches:
        created = list(DailySlot.objects.bulk_create(rows, ignore_conflicts=True))
    else:
        expected = {
            (r.slot_master_id, r.date): (r.status, r.blocked_label or None, bool(r.home_department_only))
            for r in rows
            if (r.slot_master_id, r.date) in matches
        }
        with transaction.atomic():
            created = list(DailySlot.objects.bulk_create(rows, ignore_conflicts=True))
            _link_generated_rows(matches, expected)
    _record_generated_disruptions(equipment, rows)
    return created


def _record_restored_disruptions(
    equipment: Equipment, slot_ids: list[int], status: str, actor, *, old_status: str = SlotStatus.BLOCKED
) -> None:
    """Slots a removed rule gave back: the rule's disruption (Under Maintenance etc.) ends for them; slots that go
    back Under Maintenance (equipment in repair) join that disruption."""
    from .disruption_service import (
        DisruptionInput,
        auto_recorded_slot_statuses,
        record_slot_status_change,
        record_unrecorded_slots,
    )

    statuses = set(auto_recorded_slot_statuses())
    if old_status in statuses:
        slots = list(DailySlot.objects.filter(pk__in=slot_ids))
        for slot in slots:
            slot._disruption_old_status = old_status
        record_slot_status_change(
            equipment, slots, status, DisruptionInput(user=actor, source="CHANGE_SLOT_STATUS"), log_change=False
        )
        return
    if status not in statuses:
        return
    record_unrecorded_slots(
        equipment,
        DailySlot.objects.filter(pk__in=slot_ids),
        DisruptionInput(user=actor, source="CHANGE_SLOT_STATUS"),
        old_status=old_status,
    )


def _record_generated_disruptions(equipment: Equipment, rows: list[DailySlot]) -> None:
    """Slots generated Under Maintenance (equipment in repair) join its open disruption event."""
    from .disruption_service import DisruptionInput, auto_recorded_slot_statuses, record_unrecorded_slots

    statuses = set(auto_recorded_slot_statuses())
    keys = [(r.slot_master_id, r.date) for r in rows if r.status in statuses]
    if not keys:
        return
    try:
        qs = DailySlot.objects.filter(
            slot_master_id__in={k[0] for k in keys}, date__in={k[1] for k in keys}, booking__isnull=True
        )
        record_unrecorded_slots(equipment, qs, DisruptionInput(source="EQUIPMENT_STATUS"))
    except Exception:
        logger.exception("Could not record generated disruption slots for equipment %s", equipment.pk)
