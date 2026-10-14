"""
Let users change "Quantity Required" after booking on every 3D print / 2D laser cutting equipment: sets
editing_required=True on the system Quantity Required inputs (key A, NUMERIC, that label) of fabrication
equipment. No other input, no other equipment is touched. Safe to run again.

  python manage.py make_fabrication_quantity_editable                          # dry run (read-only)
  python manage.py make_fabrication_quantity_editable --apply --confirm MAKE_QUANTITY_EDITABLE
  python manage.py make_fabrication_quantity_editable --verify                 # read-only, fails if any left

Output: counts and input / equipment ids only.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from iic_booking.equipment.fabrication import system_quantity_fields
from iic_booking.equipment.models import DynamicInputField

CONFIRM = "MAKE_QUANTITY_EDITABLE"


def _ids(values) -> str:
    return ",".join(str(v) for v in values) or "-"


class Command(BaseCommand):
    help = "Make the fabrication Quantity Required inputs editable after booking. Dry run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write editing_required=True.")
        parser.add_argument("--confirm", default="", help=f"Required with --apply: {CONFIRM}")
        parser.add_argument("--verify", action="store_true", help="Read-only; fails if any input is still locked.")

    def handle(self, *args, **options):
        apply, verify = bool(options["apply"]), bool(options["verify"])
        if apply and verify:
            raise CommandError("Use either --apply or --verify.")
        if apply and options["confirm"] != CONFIRM:
            raise CommandError(f"--apply needs --confirm {CONFIRM}")
        mode = "apply" if apply else "verify" if verify else "dry-run"
        self.stdout.write(f"MODE={mode}")

        with transaction.atomic():
            rows = system_quantity_fields()
            locked = [row for row in rows if not row.editing_required]
            self.stdout.write(
                f"QUANTITY_INPUTS total={len(rows)} equipment={len({row.equipment_id for row in rows})} "
                f"already_editable={len(rows) - len(locked)} locked={len(locked)}"
            )
            self.stdout.write(f"LOCKED_INPUT_IDS {_ids(row.id for row in locked)}")
            self.stdout.write(f"LOCKED_EQUIPMENT_IDS {_ids(sorted({row.equipment_id for row in locked}))}")
            if apply:
                updated = DynamicInputField.objects.filter(
                    id__in=[row.id for row in locked], editing_required=False
                ).update(editing_required=True)
                self.stdout.write(f"UPDATED {updated}")

        if verify and locked:
            raise CommandError(f"{len(locked)} Quantity Required input(s) still locked")
        if verify:
            self.stdout.write("VERIFY OK")
