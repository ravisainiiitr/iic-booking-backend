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


def schedule_covers_datetime(sched: EquipmentModeSchedule, on_date: date, at_time: Optional[time] = None) -> bool:
    if on_date < sched.start_date or on_date > sched.end_date:
        return False
    if sched.start_time and sched.end_time and at_time is not None:
        return sched.start_time <= at_time <= sched.end_time
    return True


def schedules_covering_date(
    parent_id: int,
    on_date: date,
    *,
    mode_equipment_id: Optional[int] = None,
) -> QuerySet:
    qs = EquipmentModeSchedule.objects.filter(
        parent_equipment_id=parent_id,
        start_date__lte=on_date,
        end_date__gte=on_date,
    )
    if mode_equipment_id is not None:
        qs = qs.filter(mode_equipment_id=mode_equipment_id)
    return qs


def exclusive_schedule_for_slot(
    parent_id: int, on_date: date, at_time: Optional[time] = None
) -> Optional[EquipmentModeSchedule]:
    for sched in (
        schedules_covering_date(parent_id, on_date)
        .filter(behavior=ModeScheduleBehavior.EXCLUSIVE)
        .select_related("mode_equipment")
        .order_by("id")
    ):
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
    for sched in schedules_covering_date(
        parent_id, on_date, mode_equipment_id=equipment.equipment_id
    ).order_by("id"):
        if schedule_covers_datetime(sched, on_date, at_time):
            return sched
    return None


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
    return not schedules_covering_date(parent_id, on_date).filter(
        behavior=ModeScheduleBehavior.EXCLUSIVE
    ).exists()


def equipment_bookable_on_date(
    equipment: Equipment, on_date: date, at_time: Optional[time] = None
) -> tuple[bool, str]:
    if not multimode_enabled_for_equipment(equipment):
        return True, ""

    parent = resolve_mode_parent(equipment)
    parent_id = parent.equipment_id

    if equipment.parent_equipment_id:
        active = active_mode_schedule_for_child(equipment, on_date, at_time)
        if active is None:
            return False, "This equipment mode is not scheduled for booking at the selected time."
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
    exclusive_parent_ids = list(
        EquipmentModeSchedule.objects.filter(
            behavior=ModeScheduleBehavior.EXCLUSIVE,
            start_date__lte=on_date,
            end_date__gte=on_date,
        ).values_list("parent_equipment_id", flat=True)
    )
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


def slot_mode_overlay(equipment: Equipment, slot: DailySlot) -> Optional[dict[str, Any]]:
    """
    Display overlay for end users when a slot is not bookable due to multi-mode rules.
    Returns {label, color, status} or None if no overlay.
    """
    from .models import SlotStatus

    if not multimode_enabled_for_equipment(equipment):
        return None
    if slot.status != SlotStatus.AVAILABLE:
        return None

    at_time = _slot_local_time(slot)
    on_date = slot.date

    if equipment.parent_equipment_id:
        active = active_mode_schedule_for_child(equipment, on_date, at_time)
        if active is not None:
            return None
        nearest = (
            EquipmentModeSchedule.objects.filter(mode_equipment_id=equipment.equipment_id)
            .order_by("-start_date")
            .first()
        )
        label = (nearest.unavailable_label if nearest else None) or "Mode not scheduled"
        color = (nearest.unavailable_color if nearest else None) or DEFAULT_GREY
        return {"label": label, "color": color, "status": "BLOCKED", "mode_overlay": "child_unavailable"}

    parent_id = equipment.equipment_id
    excl = exclusive_schedule_for_slot(parent_id, on_date, at_time)
    if excl is None:
        return None
    label = excl.exclusive_blocked_label or "Alternate mode active"
    color = excl.exclusive_blocked_color or DEFAULT_GREY
    return {"label": label, "color": color, "status": "BLOCKED", "mode_overlay": "exclusive_parent"}


def apply_mode_overlays_to_slot_payloads(
    equipment: Equipment, slots: list[DailySlot], serialized: list[dict]
) -> list[dict]:
    """Mutate serialized slot dicts with multi-mode display overlays (keep cells non-blank)."""
    by_id = {s.id: s for s in slots}
    for row in serialized:
        sid = row.get("id")
        slot = by_id.get(sid)
        if slot is None:
            continue
        overlay = slot_mode_overlay(equipment, slot)
        if not overlay:
            continue
        row["status"] = overlay["status"]
        row["status_display"] = overlay["label"]
        row["blocked_label"] = overlay["label"]
        row["mode_overlay_color"] = overlay["color"]
        row["mode_overlay"] = overlay.get("mode_overlay")
        row["available_for_external"] = False
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
        Equipment.objects.filter(equipment_id__in=equipment_ids).only(
            "equipment_id", "parent_equipment_id"
        )
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
