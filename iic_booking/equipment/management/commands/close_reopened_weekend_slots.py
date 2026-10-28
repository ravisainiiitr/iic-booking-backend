"""Close weekend / holiday slots that a maintenance switch-back wrongly made Available.

Before the fix, slots generated while equipment was Under Maintenance were Under Maintenance on weekends and
holidays too, and switching the equipment back to Operational turned them all Available. This puts such slots
back to Not Available — their status had it not been for the bug.

A slot is changed only when all hold:
* its date is a Saturday, Sunday or an active holiday, and it has not started yet;
* it is Available and has no booking (booked slots are left as they are);
* it was created during a maintenance period of its equipment, and that period did not convert it from Available
  (a slot staff had opened before maintenance stays open).

Dry run by default; ``--apply`` changes the slots.
"""

from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone


def reopened_weekend_slot_ids(now=None, equipment_ids=None) -> tuple[list[int], dict]:
    """(slot ids to close, details per equipment for the report)."""
    from iic_booking.equipment.models import DailySlot, EquipmentMaintenancePeriod, Holiday, SlotStatus

    now = now or timezone.now()
    periods = EquipmentMaintenancePeriod.objects.all()
    if equipment_ids:
        periods = periods.filter(equipment_id__in=equipment_ids)
    by_equipment = defaultdict(list)
    for p in periods.only("equipment_id", "started_at", "ended_at", "auto_slot_ids"):
        by_equipment[p.equipment_id].append(p)

    candidates = (
        DailySlot.objects.filter(
            slot_master__equipment_id__in=list(by_equipment),
            status=SlotStatus.AVAILABLE,
            booking__isnull=True,
            start_datetime__gt=now,
        )
        .select_related("slot_master__equipment")
        .only("id", "date", "start_datetime", "created_at", "slot_master__equipment__name",
              "slot_master__equipment__code", "slot_master__equipment_id")
    )
    rows = list(candidates)
    if not rows:
        return [], {}
    closed_days = Holiday.get_holidays_in_range(min(r.date for r in rows), max(r.date for r in rows))

    ids, report = [], defaultdict(list)
    for slot in rows:
        if slot.date not in closed_days:
            continue
        equipment_id = slot.slot_master.equipment_id
        for period in by_equipment[equipment_id]:
            ended = period.ended_at or now
            made_during = period.started_at <= slot.created_at <= ended
            was_open = slot.pk in set(period.auto_slot_ids or [])
            if made_during and not was_open:
                ids.append(slot.pk)
                eq = slot.slot_master.equipment
                report[f"{eq.code} {eq.name}"].append(
                    f"{timezone.localtime(slot.start_datetime):%a %d %b %Y %H:%M} ({closed_days[slot.date]['reason']})"
                )
                break
    return ids, dict(report)


class Command(BaseCommand):
    help = "Close weekend / holiday slots that switching equipment back from maintenance wrongly made Available."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Change the slots (default: dry run)")
        parser.add_argument("--equipment", type=int, action="append", help="Only this equipment id (repeatable)")

    def handle(self, *args, **opts):
        from iic_booking.equipment.models import DailySlot, SlotStatus

        ids, report = reopened_weekend_slot_ids(equipment_ids=opts.get("equipment"))
        for equipment, slots in sorted(report.items()):
            self.stdout.write(f"{equipment}: {len(slots)} slot(s)")
            for line in slots:
                self.stdout.write(f"  {line}")
        self.stdout.write(f"Total: {len(ids)} weekend/holiday slot(s) to close")
        if not opts["apply"]:
            self.stdout.write("Dry run: nothing changed. Run with --apply to close them.")
            return
        with transaction.atomic():
            # Re-check at write time: still Available and unbooked.
            changed = DailySlot.objects.filter(pk__in=ids, status=SlotStatus.AVAILABLE, booking__isnull=True).update(
                status=SlotStatus.NOT_AVAILABLE
            )
        self.stdout.write(self.style.SUCCESS(f"Closed {changed} slot(s) (Available -> Not Available)."))
