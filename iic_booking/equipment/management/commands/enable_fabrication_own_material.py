"""
Offer "I will bring my own sheet material" / "I will bring my own printing material" on every 3D print and 2D
laser cutting equipment: equipment whose own-material fixed charge is blank (option hidden) gets 0, i.e. no
material charge while machine time is still charged. Equipment with a charge already set keeps it.

Only the equipment setting changes; bookings and charges are not touched. Safe to run again.
Output: equipment ids, codes, profiles and counts only.

  python manage.py enable_fabrication_own_material            # dry run
  python manage.py enable_fabrication_own_material --apply
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from iic_booking.equipment.models import FABRICATION_PROFILE_TYPES, Equipment

DEFAULT_CHARGE = Decimal("0.00")


class Command(BaseCommand):
    help = "Enable the own-material option on 3D print and laser cutting equipment. Dry run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Set the blank own-material charges to 0.")

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        self.stdout.write(f"MODE={'apply' if apply else 'dry-run'}")
        equipment_qs = Equipment.objects.filter(profile_type__in=FABRICATION_PROFILE_TYPES).order_by("equipment_id")
        to_enable = []
        already = 0
        for equipment in equipment_qs.only("equipment_id", "code", "profile_type", "own_material_fixed_charge"):
            if equipment.own_material_fixed_charge is None:
                to_enable.append(equipment)
                state = "enabled" if apply else "to_enable"
                self.stdout.write(
                    f"EQUIPMENT id={equipment.equipment_id} code={equipment.code} profile={equipment.profile_type} "
                    f"charge=blank -> {DEFAULT_CHARGE} {state}"
                )
            else:
                already += 1
                self.stdout.write(
                    f"EQUIPMENT id={equipment.equipment_id} code={equipment.code} profile={equipment.profile_type} "
                    f"charge={equipment.own_material_fixed_charge} already_enabled"
                )
        if apply and to_enable:
            with transaction.atomic():
                updated = Equipment.objects.filter(
                    pk__in=[e.pk for e in to_enable], own_material_fixed_charge__isnull=True
                ).update(own_material_fixed_charge=DEFAULT_CHARGE)
        else:
            updated = 0
        self.stdout.write(
            f"SUMMARY fabrication_equipment={len(to_enable) + already} "
            f"{'enabled' if apply else 'to_enable'}={updated if apply else len(to_enable)} already_enabled={already}"
        )
