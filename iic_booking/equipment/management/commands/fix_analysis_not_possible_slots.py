"""Relabel slots freed from an "Analysis Not Possible" booking that were wrongly marked Operator Absent.

Before the fix, a booking flagged Analysis Not Possible released its slots (refund, reschedule, deadline expiry)
as Operator Absent. Such a slot is still linked to the Analysis Not Possible disruption (type Other, recorded from
Booking details) and to no Operator Absent disruption. This command sets those slots to Other Reasons with the
label "Analysis Not Possible". Dry run unless --apply.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.maintenance_policy import ANALYSIS_NOT_POSSIBLE_SLOT_LABEL
from iic_booking.equipment.models import DailySlot, DisruptionEventSlot, SlotStatus


def wrongly_marked_slots(*, include_past: bool = False):
    anp_slot_ids = DisruptionEventSlot.objects.filter(
        released_at__isnull=True,
        event__disruption_type="OTHER",
        event__source="BOOKING_DETAILS",
        daily_slot__isnull=False,
    ).values_list("daily_slot_id", flat=True)
    operator_absent_ids = DisruptionEventSlot.objects.filter(
        released_at__isnull=True,
        event__disruption_type="OPERATOR_ABSENT",
        daily_slot__isnull=False,
    ).values_list("daily_slot_id", flat=True)
    qs = DailySlot.objects.filter(
        pk__in=anp_slot_ids, status=SlotStatus.OPERATOR_ABSENT, booking__isnull=True
    ).exclude(pk__in=operator_absent_ids)
    if not include_past:
        qs = qs.filter(end_datetime__gt=timezone.now())
    return qs


class Command(BaseCommand):
    help = "Set slots freed from Analysis Not Possible bookings (wrongly Operator Absent) to Other Reasons."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Change the slots (default: report only).")
        parser.add_argument("--include-past", action="store_true", help="Also relabel slots that have ended.")

    def handle(self, *args, **options):
        qs = wrongly_marked_slots(include_past=options["include_past"])
        rows = list(qs.values_list("pk", "slot_master__equipment_id", "date").order_by("date", "pk"))
        per_equipment: dict[int, int] = {}
        for _, equipment_id, _ in rows:
            per_equipment[equipment_id] = per_equipment.get(equipment_id, 0) + 1
        self.stdout.write(f"Slots wrongly marked Operator Absent: {len(rows)}")
        for equipment_id, count in sorted(per_equipment.items()):
            self.stdout.write(f"  equipment {equipment_id}: {count}")
        if not options["apply"]:
            self.stdout.write("Dry run: nothing changed. Add --apply to relabel them.")
            return
        with transaction.atomic():
            updated = DailySlot.objects.filter(
                pk__in=[r[0] for r in rows], status=SlotStatus.OPERATOR_ABSENT, booking__isnull=True
            ).update(status=SlotStatus.BLOCKED, blocked_label=ANALYSIS_NOT_POSSIBLE_SLOT_LABEL)
        self.stdout.write(self.style.SUCCESS(f"Relabelled {updated} slot(s) as {ANALYSIS_NOT_POSSIBLE_SLOT_LABEL}."))
