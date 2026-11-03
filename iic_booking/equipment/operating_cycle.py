"""Operating day that does not start at midnight ("Operating day starts at" on the equipment).

With a 09:00 start and one-hour Slot Masters 09:00-10:00 ... 08:00-09:00, the operating day D has 24 slots from
D 09:00 to D+1 09:00: slots that open before 09:00 run on the next calendar day but keep ``DailySlot.date = D``.
Holidays and weekends follow D, so a Friday cycle runs into Saturday morning and Monday's 00:00-09:00 belongs to
Monday's cycle (Tuesday morning). Generation itself is ``SlotGenerator._aware_slot_datetimes``.

Changing the start re-times upcoming slots that nobody holds; it is refused while an upcoming booked or held slot
would move (see ``booked_slots_that_would_move``).
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_save, pre_save
from django.utils import timezone

logger = logging.getLogger(__name__)


def _upcoming_slots(equipment):
    from .models import DailySlot

    return DailySlot.objects.filter(slot_master__equipment=equipment, date__gte=timezone.localdate()).select_related(
        "slot_master"
    )


def _wanted(slot, day_start):
    from .slot_utils import SlotGenerator

    master = slot.slot_master
    return SlotGenerator._aware_slot_datetimes(slot.date, master.open_time, master.close_time, day_start)


def _held(slot) -> bool:
    from .models import SlotStatus

    return slot.booking_id is not None or slot.status in (SlotStatus.BOOKED, SlotStatus.BOOKING_NOT_UTILIZED)


def booked_slots_that_would_move(equipment, new_day_start) -> list[str]:
    """Labels of upcoming slots held by a booking that a new operating day start would move."""
    out = []
    for slot in _upcoming_slots(equipment):
        if not _held(slot) or slot.slot_master is None:
            continue
        start, end = _wanted(slot, new_day_start)
        if start != slot.start_datetime or end != slot.end_datetime:
            out.append(f"{slot.date} {slot.slot_master.open_time:%H:%M}")
    return out


def retime_upcoming_slots(equipment) -> int:
    """Move upcoming slots nobody holds to the times of the current operating day start."""
    from .models import DailySlot

    moved = 0
    for slot in _upcoming_slots(equipment):
        if _held(slot) or slot.slot_master is None:
            continue
        start, end = _wanted(slot, equipment.operating_day_start)
        if start != slot.start_datetime or end != slot.end_datetime:
            DailySlot.objects.filter(pk=slot.pk).update(start_datetime=start, end_datetime=end)
            moved += 1
    if moved:
        logger.info("operating day start: re-timed %s upcoming slots of equipment %s", moved, equipment.pk)
    return moved


def _remember(sender, instance, raw=False, **kwargs):
    if raw or instance.pk is None:
        instance._operating_day_start_before = None
        return
    instance._operating_day_start_before = (
        sender._base_manager.filter(pk=instance.pk).values_list("operating_day_start", flat=True).first()
    )


def _after_save(sender, instance, created=False, raw=False, **kwargs):
    if raw or created:
        return
    before = getattr(instance, "_operating_day_start_before", None)
    if before != instance.operating_day_start:
        retime_upcoming_slots(instance)


def connect_signals() -> None:
    from .models import Equipment

    pre_save.connect(_remember, sender=Equipment, weak=False, dispatch_uid="operating_cycle_pre")
    post_save.connect(_after_save, sender=Equipment, weak=False, dispatch_uid="operating_cycle_post")
