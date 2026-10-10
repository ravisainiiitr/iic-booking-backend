"""
Multi-mode equipment helpers: catalog visibility, slot overlays, conflicts.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Iterable, Optional, Sequence

from django.db.models import QuerySet
from django.utils import timezone

from iic_booking.users.models.user_type import UserType

from .models import (
    BookingStatus,
    DailySlot,
    Equipment,
    EquipmentModeSchedule,
    ModeAvailability,
    ModeScheduleBehavior,
)

DEFAULT_GREY = "#9ca3af"

_OCCUPYING_BOOKING_STATUSES = (
    BookingStatus.PENDING,
    BookingStatus.PENDING_PAYMENT,
    BookingStatus.BOOKED,
    BookingStatus.HOLD,
    BookingStatus.DISRUPTION_PENDING,
    BookingStatus.UNDER_MAINTENANCE,
    BookingStatus.OTHER_DISRUPTION,
    BookingStatus.WAITLISTED,
)


def is_staff_bypass_user(user) -> bool:
    """Admin-panel users see all modes in catalog/slots for management."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return getattr(user, "user_type", None) in UserType.get_admin_panel_codes()


def bypasses_multimode_restrictions(user) -> bool:
    """Admin and OIC always book/access every mode without schedule restrictions."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return getattr(user, "user_type", None) in (UserType.ADMIN, UserType.MANAGER)


def resolve_mode_parent(equipment: Equipment) -> Equipment:
    if equipment.parent_equipment_id:
        parent = getattr(equipment, "parent_equipment", None)
        if parent is not None:
            return parent
        return Equipment.objects.get(pk=equipment.parent_equipment_id)
    return equipment


def multimode_enabled_for_equipment(equipment: Equipment) -> bool:
    """
    Multi-mode rules apply only when the base (parent) instrument has
    enable_multi_mode=True. Otherwise treat all as standalone instruments.
    """
    parent = resolve_mode_parent(equipment)
    return bool(getattr(parent, "enable_multi_mode", False))


def mode_family_ids(equipment: Equipment) -> list[int]:
    parent = resolve_mode_parent(equipment)
    pid = parent.equipment_id
    child_ids = list(
        Equipment.objects.filter(parent_equipment_id=pid).values_list("equipment_id", flat=True)
    )
    return [pid] + [cid for cid in child_ids if cid != pid]


def _slot_local_time(slot: DailySlot) -> Optional[time]:
    if not slot.start_datetime:
        return None
    dt = slot.start_datetime
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.time()


def schedule_covers_date(sched: EquipmentModeSchedule, on_date: date) -> bool:
    """Date range (blank dates = no limit) plus the optional weekly repeat (empty weekdays = every day)."""
    return sched.covers_date(on_date)


def schedule_covers_datetime(sched: EquipmentModeSchedule, on_date: date, at_time: Optional[time] = None) -> bool:
    if not schedule_covers_date(sched, on_date):
        return False
    if sched.start_time and sched.end_time and at_time is not None:
        return sched.start_time <= at_time <= sched.end_time
    return True


def schedules_covering_date(
    parent_id: int,
    on_date: date,
    *,
    mode_equipment_id: Optional[int] = None,
    behavior: Optional[str] = None,
) -> list[EquipmentModeSchedule]:
    """Schedules of the family active on ``on_date`` (date range and weekly repeat), ordered by id."""
    qs = EquipmentModeSchedule.objects.filter(
        EquipmentModeSchedule.covering_date_q(on_date),
        parent_equipment_id=parent_id,
    )
    if mode_equipment_id is not None:
        qs = qs.filter(mode_equipment_id=mode_equipment_id)
    if behavior is not None:
        qs = qs.filter(behavior=behavior)
    return [s for s in qs.select_related("mode_equipment").order_by("id") if schedule_covers_date(s, on_date)]


def exclusive_schedule_for_slot(
    parent_id: int,
    on_date: date,
    at_time: Optional[time] = None,
    *,
    exclude_mode_id: Optional[int] = None,
) -> Optional[EquipmentModeSchedule]:
    for sched in schedules_covering_date(parent_id, on_date, behavior=ModeScheduleBehavior.EXCLUSIVE):
        if exclude_mode_id is not None and sched.mode_equipment_id == exclude_mode_id:
            continue
        if schedule_covers_datetime(sched, on_date, at_time):
            return sched
    return None


def exclusive_schedule_for_date(parent_id: int, on_date: date) -> Optional[EquipmentModeSchedule]:
    return exclusive_schedule_for_slot(parent_id, on_date, None)


def active_mode_schedule_for_child(
    equipment: Equipment, on_date: date, at_time: Optional[time] = None
) -> Optional[EquipmentModeSchedule]:
    if not equipment.parent_equipment_id:
        return None
    parent_id = equipment.parent_equipment_id
    for sched in schedules_covering_date(parent_id, on_date, mode_equipment_id=equipment.equipment_id):
        if schedule_covers_datetime(sched, on_date, at_time):
            return sched
    return None


def mode_requires_schedule(equipment: Equipment) -> bool:
    return bool(equipment.parent_equipment_id) and (
        getattr(equipment, "mode_availability", ModeAvailability.ALWAYS) == ModeAvailability.SCHEDULED_ONLY
    )


def is_equipment_visible_on_date(equipment: Equipment, on_date: date) -> bool:
    """
    Catalog visibility for end users.
    When multi-mode is enabled for the family: children stay searchable; parents hide only
    on days fully covered by exclusive (catalog uses date-only).
    When multi-mode is disabled: everyone is treated as a standalone parent.
    """
    if not multimode_enabled_for_equipment(equipment):
        return True

    is_child = bool(equipment.parent_equipment_id)
    if is_child:
        # Children remain searchable whenever multi-mode is enabled for the OIC.
        return True

    parent_id = equipment.equipment_id
    if not Equipment.objects.filter(parent_equipment_id=parent_id).exists():
        return True
    # Hide parent from catalog on dates with any exclusive schedule covering that date
    return not schedules_covering_date(parent_id, on_date, behavior=ModeScheduleBehavior.EXCLUSIVE)


def equipment_bookable_on_date(
    equipment: Equipment, on_date: date, at_time: Optional[time] = None
) -> tuple[bool, str]:
    if not multimode_enabled_for_equipment(equipment):
        return True, ""

    parent = resolve_mode_parent(equipment)
    parent_id = parent.equipment_id

    if equipment.parent_equipment_id:
        if mode_requires_schedule(equipment) and active_mode_schedule_for_child(equipment, on_date, at_time) is None:
            return False, "This equipment mode is not scheduled for booking at the selected time."
        if exclusive_schedule_for_slot(parent_id, on_date, at_time, exclude_mode_id=equipment.equipment_id):
            return (
                False,
                "Another mode of this instrument is running exclusively at this time.",
            )
        return True, ""

    excl = exclusive_schedule_for_slot(parent_id, on_date, at_time)
    if excl is not None:
        return (
            False,
            "Only the active exclusive mode of this instrument can be booked at this time.",
        )
    return True, ""


def requires_exclusive_family_conflict(
    equipment: Equipment, on_date: date, at_time: Optional[time] = None
) -> bool:
    if not multimode_enabled_for_equipment(equipment):
        return False
    parent = resolve_mode_parent(equipment)
    return exclusive_schedule_for_slot(parent.equipment_id, on_date, at_time) is not None


def family_slots_overlap_conflict(
    equipment: Equipment,
    starts_at: datetime,
    ends_at: datetime,
    *,
    exclude_slot_ids: Optional[Sequence[int]] = None,
) -> Optional[DailySlot]:
    on_date = timezone.localtime(starts_at).date() if timezone.is_aware(starts_at) else starts_at.date()
    at_time = timezone.localtime(starts_at).time() if timezone.is_aware(starts_at) else starts_at.time()
    if not requires_exclusive_family_conflict(equipment, on_date, at_time):
        return None

    family = mode_family_ids(equipment)
    sibling_ids = [eid for eid in family if eid != equipment.equipment_id]
    if not sibling_ids:
        return None

    qs = DailySlot.objects.filter(
        slot_master__equipment_id__in=sibling_ids,
        booking__isnull=False,
        booking__status__in=_OCCUPYING_BOOKING_STATUSES,
        start_datetime__lt=ends_at,
        end_datetime__gt=starts_at,
    ).select_related("booking", "slot_master")
    if exclude_slot_ids:
        qs = qs.exclude(id__in=list(exclude_slot_ids))
    return qs.order_by("start_datetime").first()


def filter_queryset_for_mode_catalog(queryset: QuerySet, user, *, on_date: Optional[date] = None) -> QuerySet:
    """
    Catalog filter for end users.
    - Multi-mode disabled families: no filter (children appear as normal equipment).
    - Multi-mode enabled: hide parents on exclusive-today; children stay visible.
    """
    if is_staff_bypass_user(user) or bypasses_multimode_restrictions(user):
        return queryset
    on_date = on_date or timezone.localdate()

    # Parents with exclusive schedule today AND multi-mode enabled for that parent
    exclusive_parent_ids = [
        sched.parent_equipment_id
        for sched in EquipmentModeSchedule.objects.filter(
            EquipmentModeSchedule.covering_date_q(on_date),
            behavior=ModeScheduleBehavior.EXCLUSIVE,
        ).only("parent_equipment_id", "start_date", "end_date", "weekdays")
        if schedule_covers_date(sched, on_date)
    ]
    hide_parent_ids = []
    if exclusive_parent_ids:
        for pid in set(exclusive_parent_ids):
            try:
                eq = Equipment.objects.get(pk=pid)
            except Equipment.DoesNotExist:
                continue
            if multimode_enabled_for_equipment(eq):
                hide_parent_ids.append(pid)

    if hide_parent_ids:
        queryset = queryset.exclude(equipment_id__in=hide_parent_ids)
    return queryset


def _family_schedules(equipment: Equipment) -> list[EquipmentModeSchedule]:
    parent_id = equipment.parent_equipment_id or equipment.equipment_id
    return list(EquipmentModeSchedule.objects.filter(parent_equipment_id=parent_id).order_by("id"))


def _first_active(
    schedules: Sequence[EquipmentModeSchedule],
    on_date: date,
    at_time: Optional[time],
    *,
    mode_id: Optional[int] = None,
    exclude_mode_id: Optional[int] = None,
    behavior: Optional[str] = None,
) -> Optional[EquipmentModeSchedule]:
    for sched in schedules:
        if mode_id is not None and sched.mode_equipment_id != mode_id:
            continue
        if exclude_mode_id is not None and sched.mode_equipment_id == exclude_mode_id:
            continue
        if behavior is not None and sched.behavior != behavior:
            continue
        if schedule_covers_datetime(sched, on_date, at_time):
            return sched
    return None


def slot_mode_overlay(
    equipment: Equipment,
    slot: DailySlot,
    schedules: Optional[Sequence[EquipmentModeSchedule]] = None,
) -> Optional[dict[str, Any]]:
    """
    Display overlay for end users when a slot is not bookable due to multi-mode rules.
    Returns {label, color, status} or None if no overlay. ``schedules`` (all schedules of the family,
    ordered by id) can be passed to avoid reloading them for every slot.
    """
    from .models import SlotStatus

    if not multimode_enabled_for_equipment(equipment):
        return None
    if slot.status != SlotStatus.AVAILABLE:
        return None
    if schedules is None:
        schedules = _family_schedules(equipment)

    at_time = _slot_local_time(slot)
    on_date = slot.date
    exclusive = ModeScheduleBehavior.EXCLUSIVE

    if equipment.parent_equipment_id:
        mode_id = equipment.equipment_id
        if mode_requires_schedule(equipment) and _first_active(schedules, on_date, at_time, mode_id=mode_id) is None:
            own = [s for s in schedules if s.mode_equipment_id == mode_id]
            nearest = max(own, key=lambda s: (s.start_date is not None, s.start_date or date.min, s.id), default=None)
            label = (nearest.unavailable_label if nearest else None) or "Mode not scheduled"
            color = (nearest.unavailable_color if nearest else None) or DEFAULT_GREY
            return {"label": label, "color": color, "status": "BLOCKED", "mode_overlay": "child_unavailable"}
        sibling_excl = _first_active(schedules, on_date, at_time, exclude_mode_id=mode_id, behavior=exclusive)
        if sibling_excl is None:
            return None
        label = sibling_excl.exclusive_blocked_label or "Alternate mode active"
        color = sibling_excl.exclusive_blocked_color or DEFAULT_GREY
        return {"label": label, "color": color, "status": "BLOCKED", "mode_overlay": "exclusive_sibling"}

    excl = _first_active(schedules, on_date, at_time, behavior=exclusive)
    if excl is None:
        return None
    label = excl.exclusive_blocked_label or "Alternate mode active"
    color = excl.exclusive_blocked_color or DEFAULT_GREY
    return {"label": label, "color": color, "status": "BLOCKED", "mode_overlay": "exclusive_parent"}


def _overlays_by_slot_id(equipment: Equipment, slots: list[DailySlot]) -> dict[int, dict[str, Any]]:
    if not slots or not multimode_enabled_for_equipment(equipment):
        return {}
    schedules = _family_schedules(equipment)
    out = {}
    for slot in slots:
        overlay = slot_mode_overlay(equipment, slot, schedules)
        if overlay:
            out[slot.id] = overlay
    return out


def apply_mode_overlays_to_slot_payloads(
    equipment: Equipment, slots: list[DailySlot], serialized: list[dict]
) -> list[dict]:
    """Mutate serialized slot dicts with multi-mode display overlays (keep cells non-blank)."""
    overlays = _overlays_by_slot_id(equipment, slots)
    for row in serialized:
        overlay = overlays.get(row.get("id"))
        if not overlay:
            continue
        row["status"] = overlay["status"]
        row["status_display"] = overlay["label"]
        row["blocked_label"] = overlay["label"]
        row["mode_overlay_color"] = overlay["color"]
        row["mode_overlay"] = overlay.get("mode_overlay")
        row["available_for_external"] = False
    return serialized


_USER_BLOCK_REASONS = {
    "child_unavailable": "No schedule of this mode covers this time, so users cannot book it.",
    "exclusive_sibling": "Another mode of this instrument runs on its own at this time, so users cannot book this mode.",
    "exclusive_parent": "A mode of this instrument runs on its own at this time, so users cannot book the base instrument.",
}


def annotate_user_mode_blocks_for_staff(
    equipment: Equipment, slots: list[DailySlot], serialized: list[dict]
) -> list[dict]:
    """
    Staff keep the real slot status; Available slots that users cannot book because of the multi-mode
    setup get ``users_blocked_label`` (the label users see) and ``users_blocked_reason``.
    """
    overlays = _overlays_by_slot_id(equipment, slots)
    for row in serialized:
        overlay = overlays.get(row.get("id"))
        if not overlay:
            continue
        row["users_blocked_label"] = overlay["label"]
        row["users_blocked_reason"] = _USER_BLOCK_REASONS.get(overlay.get("mode_overlay") or "", overlay["label"])
    return serialized


def filter_slots_for_mode_dates(slots: Iterable[DailySlot], equipment: Equipment) -> list[DailySlot]:
    """
    Do not drop slots for multi-mode — keep them visible with overlays.
    Returns all slots unchanged (overlays applied at serialize time).
    """
    return list(slots)


def expand_equipment_ids_for_mode_rollup(equipment_ids: Sequence[int]) -> tuple[list[int], dict[int, int]]:
    if not equipment_ids:
        return [], {}

    eqs = list(
        Equipment.objects.filter(equipment_id__in=equipment_ids)
        .select_related("parent_equipment")
        .only("equipment_id", "enable_multi_mode", "parent_equipment", "parent_equipment__enable_multi_mode")
    )
    # Only roll up families where multi-mode is enabled
    parent_ids: set[int] = set()
    for eq in eqs:
        if not multimode_enabled_for_equipment(eq):
            continue
        if eq.parent_equipment_id:
            parent_ids.add(eq.parent_equipment_id)
        else:
            parent_ids.add(eq.equipment_id)

    children = list(
        Equipment.objects.filter(parent_equipment_id__in=parent_ids).values_list(
            "equipment_id", "parent_equipment_id"
        )
    ) if parent_ids else []

    rollup: dict[int, int] = {}
    expanded: set[int] = set()
    for pid in parent_ids:
        rollup[pid] = pid
        expanded.add(pid)
    for cid, pid in children:
        rollup[cid] = pid
        expanded.add(cid)

    for eq in eqs:
        eid = eq.equipment_id
        if eid not in rollup:
            rollup[eid] = eid
            expanded.add(eid)

    return sorted(expanded), rollup
