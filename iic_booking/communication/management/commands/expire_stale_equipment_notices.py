"""
Close automatic "Under Maintenance" notices whose equipment is Operational again.

Dry run by default; pass --apply to change rows. Only notices created by the equipment status
change (source EQUIPMENT_UNAVAILABLE) that are still open are touched: published ones become
inactive and expire now, drafts / pending requests are auto-rejected. Nothing is deleted and manual
notices are never changed. Safe to run repeatedly. Output has counts, ids and statuses only.
"""

from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from iic_booking.communication.notice_board_service import (
    expire_equipment_linked_notices,
    stale_equipment_status_notices,
)


class Command(BaseCommand):
    help = "Close open automatic equipment notices whose equipment is Operational again (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Change the rows (default: dry run).")

    def handle(self, *args, **options):
        apply = options["apply"]
        stale = list(stale_equipment_status_notices().select_related("equipment").order_by("notice_id"))
        self.stdout.write(f"mode={'apply' if apply else 'dry-run'}")
        self.stdout.write(f"stale_open_notices={len(stale)}")
        self.stdout.write(f"by_approval={dict(Counter(n.approval_status for n in stale))}")
        for n in stale:
            self.stdout.write(
                f"  notice={n.notice_id} approval={n.approval_status} active={n.is_active} "
                f"equipment={n.equipment_id} equipment_status={n.equipment.status}"
            )
        if not apply:
            self.stdout.write("dry run: no changes made")
            return

        closed = 0
        with transaction.atomic():
            for equipment in {n.equipment_id: n.equipment for n in stale}.values():
                closed += expire_equipment_linked_notices(equipment=equipment)
        self.stdout.write(f"closed={closed}")
        self.stdout.write(f"stale_open_notices_after={stale_equipment_status_notices().count()}")
