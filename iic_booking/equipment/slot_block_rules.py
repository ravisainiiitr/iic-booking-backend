"""
Recurring slot block rules ("Repeat block" on the Change slot status page).

A rule blocks slots on chosen weekdays and slot start times (local time) within a date range:
existing slots when the rule is created, and new slots as they are generated later. Only slots that
are AVAILABLE, unbooked and not yet started are ever blocked; booked slots and slots in any other
status are skipped and reported. Nothing here cancels or refunds a booking.

Each slot a rule blocks is linked to it (RecurringSlotBlockRuleSlot), so removing the rule unblocks
only its own future, still-unbooked slots. A slot also covered by another active rule stays blocked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.dept_admin_actions import record_staff_action
from iic_booking.equipment.models import (
    DailySlot,
    Equipment,
    Holiday,
    RecurringSlotBlockRule,
    RecurringSlotBlockRuleSlot,
    SlotMaster,
    SlotStatus,
)
from iic_booking.equipment.slot_utils import SlotGenerator

logger = logging.getLogger(__name__)

WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_RANGE_DAYS = 400
LIST_LIMIT = 500

SKIP_REASONS = {
    SlotStatus.BLOCKED: "Already blocked (Other Reasons)",
    SlotStatus.UNDER_MAINTENANCE: "Under maintenance",
    SlotStatus.OPERATOR_ABSENT: "Operator absent",
    SlotStatus.NOT_AVAILABLE: "Not available (closed day)",
    SlotStatus.BOOKING_NOT_UTILIZED: "Booking not utilized",
    SlotStatus.BOOKED: "Booked",
}
SHARED_REASON = "Already blocked by another repeat rule"

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
                "end_time": _hhmm(master.close_time),
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

    return {
        "weekdays": sorted(weekdays),
        "slot_times": sorted(slot_times),
        "start_date": start,
        "end_date": end,
        "label": label,
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
        "end_time": timezone.localtime(slot.end_datetime).strftime("%H:%M"),
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


def _classify(slots: list[DailySlot], *, exclude_rule_id: int | None = None) -> _Plan:
    plan = _Plan()
    blocked_ids = [s.pk for s in slots if s.status == SlotStatus.BLOCKED and not s.booking_id]
    shared_ids: set[int] = set()
    if blocked_ids:
        links = RecurringSlotBlockRuleSlot.objects.filter(daily_slot_id__in=blocked_ids, rule__is_active=True)
        if exclude_rule_id:
            links = links.exclude(rule_id=exclude_rule_id)
        shared_ids = set(links.values_list("daily_slot_id", flat=True))
    for slot in slots:
        if slot.booking_id:
            plan.booked.append(slot)
        elif slot.status == SlotStatus.AVAILABLE:
            plan.to_block.append(slot)
        elif slot.pk in shared_ids:
            plan.shared.append(slot)
            plan.other.append((slot, SHARED_REASON))
        else:
            plan.other.append((slot, SKIP_REASONS.get(slot.status, str(slot.status))))
    return plan


def _future_slot_count(equipment: Equipment, cleaned: dict) -> int:
    """Matching slots not generated yet that would be created AVAILABLE (and so get blocked by the rule)."""
    if SlotGenerator._get_initial_slot_status_for_equipment(equipment) != SlotStatus.AVAILABLE:
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
        if current.weekday() in weekdays and current not in holidays:
            for master in masters:
                if (master.pk, current) in existing:
                    continue
                slot_start, _ = SlotGenerator._aware_slot_datetimes(current, master.open_time, master.close_time)
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
    """Dry run of create_rule: what would be blocked and skipped. Writes nothing."""
    plan = _classify(_candidate_slots(equipment, cleaned))
    return _plan_summary(plan, equipment, cleaned)


def create_rule(equipment: Equipment, cleaned: dict, actor) -> tuple[RecurringSlotBlockRule, dict]:
    """Save the rule and block its matching AVAILABLE slots now, in one transaction."""
    with transaction.atomic():
        plan = _classify(_candidate_slots(equipment, cleaned, lock=True))
        rule = RecurringSlotBlockRule.objects.create(
            equipment=equipment,
            weekdays=cleaned["weekdays"],
            slot_times=cleaned["slot_times"],
            start_date=cleaned["start_date"],
            end_date=cleaned["end_date"],
            label=cleaned["label"],
            created_by=actor if getattr(actor, "pk", None) else None,
        )
        block_ids = [s.pk for s in plan.to_block]
        blocked = 0
        if block_ids:
            blocked = DailySlot.objects.filter(
                pk__in=block_ids, status=SlotStatus.AVAILABLE, booking__isnull=True
            ).update(status=SlotStatus.BLOCKED, blocked_label=_label_value(cleaned["label"]))
        links = [RecurringSlotBlockRuleSlot(rule=rule, daily_slot_id=pk, source=Source.CREATED) for pk in block_ids]
        links += [RecurringSlotBlockRuleSlot(rule=rule, daily_slot_id=s.pk, source=Source.SHARED) for s in plan.shared]
        if links:
            RecurringSlotBlockRuleSlot.objects.bulk_create(links, ignore_conflicts=True)
        summary = _plan_summary(plan, equipment, cleaned)
        summary["blocked_count"] = blocked
        rule.summary = {k: v for k, v in summary.items() if k != "slots_exist_until"}
        rule.save(update_fields=["summary"])

    record_staff_action(
        actor,
        "recurring_slot_block_rule_created",
        equipment_id=equipment.pk,
        rule_id=rule.pk,
        weekdays=cleaned["weekdays"],
        slot_times=cleaned["slot_times"],
        start_date=str(cleaned["start_date"]),
        end_date=str(cleaned["end_date"]),
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
    other_rule_by_slot: dict[int, RecurringSlotBlockRule] = {}
    for link in (
        RecurringSlotBlockRuleSlot.objects.filter(daily_slot_id__in=slot_ids, rule__is_active=True)
        .exclude(rule_id=rule.pk)
        .select_related("rule")
        .order_by("rule__created_at", "rule_id")
    ):
        other_rule_by_slot.setdefault(link.daily_slot_id, link.rule)
    own_label = rule.label or ""
    for slot in slots_qs:
        if slot.status != SlotStatus.BLOCKED or slot.booking_id:
            plan.unchanged.append(slot)
        elif slot.pk in other_rule_by_slot:
            plan.kept.append((slot, other_rule_by_slot[slot.pk]))
        elif (slot.blocked_label or "") == own_label:
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


def remove_rule(rule: RecurringSlotBlockRule, actor) -> dict:
    """Deactivate the rule and unblock its own future, unbooked slots no other active rule covers."""
    equipment = rule.equipment
    with transaction.atomic():
        rule = RecurringSlotBlockRule.objects.select_for_update().get(pk=rule.pk)
        if not rule.is_active:
            raise RuleInputError("This repeat block was already removed.", code="already_removed")
        plan = _removal_plan(rule, lock=True)
        restore_status = SlotGenerator._get_initial_slot_status_for_equipment(equipment)
        unblocked = 0
        if plan.unblock:
            unblocked = DailySlot.objects.filter(
                pk__in=[s.pk for s in plan.unblock], status=SlotStatus.BLOCKED, booking__isnull=True
            ).update(status=restore_status, blocked_label=None)
        own_label = rule.label or ""
        handover: dict[int, tuple[RecurringSlotBlockRule, list[int]]] = {}
        for slot, other in plan.kept:
            if (slot.blocked_label or "") == own_label:
                handover.setdefault(other.pk, (other, []))[1].append(slot.pk)
        for other, ids in handover.values():
            DailySlot.objects.filter(pk__in=ids, status=SlotStatus.BLOCKED).update(
                blocked_label=_label_value(other.label)
            )
        result = {
            "unblocked_count": unblocked,
            "kept_by_other_rule_count": len(plan.kept),
            "unchanged_count": len(plan.unchanged),
            "restored_status": restore_status,
        }
        rule.is_active = False
        rule.removed_by = actor if getattr(actor, "pk", None) else None
        rule.removed_at = timezone.now()
        rule.removal_summary = result
        rule.save(update_fields=["is_active", "removed_by", "removed_at", "removal_summary"])

    if unblocked and restore_status == SlotStatus.AVAILABLE:
        try:
            from iic_booking.equipment.waitlist import notify_waitlist_slots_available

            notify_waitlist_slots_available(
                equipment,
                preferred_slot_ids=[s.pk for s in plan.unblock],
                respect_reschedule_threshold=True,
            )
        except Exception as exc:
            logger.warning("Waitlist notify after removing repeat block %s failed: %s", rule.pk, exc)

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

    data = {
        "id": rule.pk,
        "equipment_id": rule.equipment_id,
        "weekdays": list(rule.weekdays or []),
        "weekday_labels": [WEEKDAY_LABELS[d] for d in (rule.weekdays or []) if 0 <= int(d) <= 6],
        "slot_times": list(rule.slot_times or []),
        "start_date": rule.start_date.isoformat(),
        "end_date": rule.end_date.isoformat(),
        "label": rule.label or "",
        "is_active": rule.is_active,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "created_by_name": _name(rule.created_by),
        "removed_at": rule.removed_at.isoformat() if rule.removed_at else None,
        "removed_by_name": _name(rule.removed_by),
        "summary": rule.summary or {},
        "removal_summary": rule.removal_summary or {},
        "blocked_now_count": rule.slot_links.filter(daily_slot__status=SlotStatus.BLOCKED)
        .exclude(source=Source.SHARED)
        .count(),
        "generated_count": rule.slot_links.filter(source=Source.GENERATED).count(),
    }
    if with_removal_preview and rule.is_active:
        data["removal_preview"] = removal_preview(rule)
    return data


# --- Slot generation hook ---------------------------------------------------------------------


def _apply_rules_to_new_rows(equipment: Equipment, rows: list[DailySlot]) -> dict[tuple[int, date], list[int]]:
    """Mark unsaved AVAILABLE rows that an active rule covers as BLOCKED; returns (slot_master_id, date) -> rule ids."""
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
    now = timezone.now()
    matches: dict[tuple[int, date], list[int]] = {}
    for row in rows:
        if row.status != SlotStatus.AVAILABLE or row.start_datetime <= now:
            continue
        hhmm = _local_start_hhmm(row)
        weekday = row.date.weekday()
        hits = [
            r
            for r in rules
            if r.start_date <= row.date <= r.end_date and weekday in (r.weekdays or []) and hhmm in (r.slot_times or [])
        ]
        if hits:
            row.status = SlotStatus.BLOCKED
            row.blocked_label = _label_value(hits[0].label)
            matches[(row.slot_master_id, row.date)] = [r.pk for r in hits]
    return matches


def _link_generated_rows(matches: dict[tuple[int, date], list[int]], labels: dict[tuple[int, date], str | None]):
    master_ids = {k[0] for k in matches}
    dates = {k[1] for k in matches}
    links = []
    for pk, master_id, slot_date, blocked_label in DailySlot.objects.filter(
        slot_master_id__in=master_ids, date__in=dates, status=SlotStatus.BLOCKED, booking__isnull=True
    ).values_list("pk", "slot_master_id", "date", "blocked_label"):
        key = (master_id, slot_date)
        if key not in matches or (blocked_label or None) != labels[key]:
            continue
        links.extend(
            RecurringSlotBlockRuleSlot(rule_id=rule_id, daily_slot_id=pk, source=Source.GENERATED)
            for rule_id in matches[key]
        )
    if links:
        RecurringSlotBlockRuleSlot.objects.bulk_create(links, ignore_conflicts=True)


def bulk_create_daily_slots(equipment: Equipment, rows: list[DailySlot]) -> list[DailySlot]:
    """bulk_create new DailySlot rows (ignore_conflicts), blocking the ones an active repeat rule covers."""
    if not rows:
        return []
    matches = _apply_rules_to_new_rows(equipment, rows)
    if not matches:
        return list(DailySlot.objects.bulk_create(rows, ignore_conflicts=True))
    labels = {(r.slot_master_id, r.date): r.blocked_label for r in rows if (r.slot_master_id, r.date) in matches}
    with transaction.atomic():
        created = list(DailySlot.objects.bulk_create(rows, ignore_conflicts=True))
        _link_generated_rows(matches, labels)
    return created
