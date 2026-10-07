"""
Give every 3D print / 2D laser cutting equipment the "Quantity Required" input (key A) for all user types:
a whole number from 1 to 1000, default 1, required. It multiplies the whole job (3D print weight and time,
laser sheet share).

A user type with its own input rows gets its own A row and a shared A row covers every other user type. An
existing A row is never changed: one that is already Quantity Required is left alone, any other meaning is
reported as a conflict and skipped. Existing bookings are not touched (they count as quantity 1).

The listing (inputs and charge profile formulas of each equipment) makes the dry run a read-only probe.
Safe to run again. Output: equipment ids, codes, user types, labels, formulas and counts only.

  python manage.py add_fabrication_quantity_input            # dry run
  python manage.py add_fabrication_quantity_input --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from iic_booking.equipment.fabrication import QUANTITY_KEY, QUANTITY_MARKER_KEY, ensure_fabrication_quantity_inputs
from iic_booking.equipment.models import (
    FABRICATION_PROFILE_TYPES,
    Booking,
    ChargeProfile,
    DynamicInputField,
    Equipment,
)


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


class Command(BaseCommand):
    help = "Add the Quantity Required input (key A) to 3D print and laser cutting equipment. Dry run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Create the missing input rows.")
        parser.add_argument(
            "--equipment-id", type=int, action="append", default=[], help="Limit to these equipment ids."
        )

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        equipment_qs = Equipment.objects.filter(profile_type__in=FABRICATION_PROFILE_TYPES).order_by("equipment_id")
        if options["equipment_id"]:
            equipment_qs = equipment_qs.filter(equipment_id__in=options["equipment_id"])

        self.stdout.write(f"MODE={'apply' if apply else 'dry-run'}")
        totals = {"equipment": 0, "created": 0, "existing": 0, "conflicts": 0}
        with transaction.atomic():
            for equipment in equipment_qs:
                totals["equipment"] += 1
                result = ensure_fabrication_quantity_inputs(equipment, apply=apply)
                totals["created"] += len(result["created"])
                totals["existing"] += len(result["existing"])
                totals["conflicts"] += len(result["conflicts"])
                self._describe(equipment, result, apply)
        self.stdout.write(
            f"SUMMARY equipment={totals['equipment']} "
            f"{'created' if apply else 'to_create'}={totals['created']} "
            f"already_present={totals['existing']} conflicts={totals['conflicts']}"
        )

    def _describe(self, equipment, result, apply):
        def names(user_types):
            return ",".join(ut or "shared" for ut in user_types) or "-"

        conflicts = ",".join(f"{ut or 'shared'}:{ft}" for ut, ft in result["conflicts"]) or "-"

        bookings = Booking.objects.filter(equipment=equipment)
        with_quantity = bookings.filter(**{f"input_values__{QUANTITY_MARKER_KEY}": True}).count()
        self.stdout.write(
            f"EQUIPMENT id={equipment.equipment_id} code={equipment.code} profile={equipment.profile_type} "
            f"bookings={bookings.count()} bookings_with_quantity={with_quantity}"
        )
        self.stdout.write(
            f"  QUANTITY {'created' if apply else 'to_create'}={names(result['created'])} "
            f"already_present={names(result['existing'])} "
            f"conflicts={conflicts}"
        )
        for field in DynamicInputField.objects.filter(equipment=equipment).order_by("user_type", "field_key"):
            limits = "/".join((field.help_text or "").splitlines()[:3]) if field.field_type == "NUMERIC" else ""
            self.stdout.write(
                f"  INPUT user_type={field.user_type or 'shared'} key={field.field_key} type={field.field_type} "
                f"required={field.is_required} default={_one_line(field.default_value) or '-'} "
                f"limits={limits or '-'} label={_one_line(field.field_label)}"
                + (" <- quantity" if field.field_key == QUANTITY_KEY else "")
            )
        for profile in ChargeProfile.objects.filter(equipment=equipment).order_by("user_type", "pricing_profile"):
            self.stdout.write(
                f"  PROFILE user_type={profile.user_type or 'shared'} pricing={profile.pricing_profile} "
                f"type={profile.profile_type} active={profile.is_active} "
                f"pc={profile.primary_unit_charge} sc={profile.secondary_unit_charge} "
                f"time_formula={_one_line(profile.time_formula) or '-'} "
                f"charge_formula={_one_line(profile.charge_formula) or '-'}"
            )
