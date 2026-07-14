"""
Repair / normalize equipment image DB paths.

Usage:
  python manage.py repair_equipment_image_paths --normalize
  python manage.py repair_equipment_image_paths --clear-missing          # dry-run
  python manage.py repair_equipment_image_paths --clear-missing --force  # apply
"""

from django.core.management.base import BaseCommand

from iic_booking.equipment.image_utils import (
    equipment_image_available,
    normalize_equipment_image_db_path,
    normalize_storage_path,
)
from iic_booking.equipment.models import Equipment


class Command(BaseCommand):
    help = "Normalize media/ prefixes or clear equipment image paths missing from storage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--normalize",
            action="store_true",
            help="Strip redundant media/ prefixes from Equipment.image names and save.",
        )
        parser.add_argument(
            "--clear-missing",
            action="store_true",
            help="List (or with --force, clear) DB paths that cannot be opened in storage.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Actually clear missing paths (required with --clear-missing to mutate DB).",
        )

    def handle(self, *args, **options):
        do_normalize = bool(options.get("normalize"))
        clear_missing = bool(options.get("clear_missing"))
        force = bool(options.get("force"))

        if not do_normalize and not clear_missing:
            self.stdout.write(
                "Nothing to do. Pass --normalize and/or --clear-missing "
                "(add --force to apply clears)."
            )
            return

        normalized = 0
        cleared = 0
        ok = 0
        missing = 0

        qs = Equipment.objects.exclude(image="").exclude(image__isnull=True)
        for equipment in qs.iterator():
            path = (equipment.image.name if equipment.image else "") or ""

            if do_normalize:
                before = path
                after = normalize_storage_path(path)
                if after != before:
                    normalize_equipment_image_db_path(equipment, save=True)
                    self.stdout.write(
                        f"Normalized equipment {equipment.equipment_id} ({equipment.code}): "
                        f"{before} -> {after}"
                    )
                    normalized += 1
                    path = after
                    equipment.refresh_from_db(fields=["image"])

            if not clear_missing:
                continue

            if equipment_image_available(equipment.image):
                ok += 1
                continue

            missing += 1
            path = (equipment.image.name if equipment.image else "") or path
            if not force:
                self.stdout.write(
                    f"Would clear equipment {equipment.equipment_id} ({equipment.code}): {path}"
                )
                cleared += 1
            else:
                equipment.image = ""
                equipment.save(update_fields=["image"])
                self.stdout.write(
                    self.style.WARNING(
                        f"Cleared missing image for equipment {equipment.equipment_id} "
                        f"({equipment.code}): {path}"
                    )
                )
                cleared += 1

        self.stdout.write(
            f"Done. ok={ok} missing={missing} normalized={normalized} cleared={cleared}"
            + (" (dry-run for clears)" if clear_missing and not force else "")
        )
