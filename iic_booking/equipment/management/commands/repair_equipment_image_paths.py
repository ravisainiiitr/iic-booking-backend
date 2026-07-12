"""
Clear equipment image DB paths that do not exist in storage.

Legacy local files were never migrated to S3, leaving paths in the database
with no backing object. Those rows make the API advertise image_url and the
proxy return 404.

Usage:
  python manage.py repair_equipment_image_paths --dry-run
  python manage.py repair_equipment_image_paths
"""

from django.core.management.base import BaseCommand

from iic_booking.equipment.image_utils import equipment_image_available
from iic_booking.equipment.models import Equipment


class Command(BaseCommand):
    help = "Clear equipment image paths that are missing from storage (S3)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report rows that would be cleared without updating the database.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        cleared = 0
        ok = 0

        qs = Equipment.objects.exclude(image="").exclude(image__isnull=True)
        for equipment in qs.iterator():
            if equipment_image_available(equipment.image):
                ok += 1
                continue

            path = equipment.image.name if equipment.image else ""
            if dry_run:
                self.stdout.write(
                    f"Would clear equipment {equipment.equipment_id} ({equipment.code}): {path}"
                )
            else:
                equipment.image = ""
                equipment.save(update_fields=["image"])
                self.stdout.write(
                    self.style.WARNING(
                        f"Cleared missing image for equipment {equipment.equipment_id} ({equipment.code}): {path}"
                    )
                )
            cleared += 1

        self.stdout.write(
            f"Done. ok={ok} cleared={cleared}" + (" (dry-run)" if dry_run else "")
        )
