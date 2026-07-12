"""
Upload equipment images from legacy local storage to S3.

Before migration 0146, equipment images were stored on the app server's local disk
(MEDIA_ROOT/equipment_images_local/). After switching to S3, re-upload any files
that exist locally but are missing from the image field's storage backend.

Usage:
  python manage.py migrate_equipment_images_to_s3 --dry-run
  python manage.py migrate_equipment_images_to_s3
"""

import os

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from iic_booking.equipment.image_utils import verify_file_field_in_storage
from iic_booking.equipment.models import Equipment


class Command(BaseCommand):
    help = "Upload legacy local equipment images to S3 (one-time after storage backend change)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be uploaded without writing to S3.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        local_root = os.path.join(settings.MEDIA_ROOT, "equipment_images_local")
        uploaded = 0
        skipped = 0
        missing = 0
        errors = 0

        qs = Equipment.objects.exclude(image="").exclude(image__isnull=True)
        for equipment in qs.iterator():
            if not equipment.image or not equipment.image.name:
                skipped += 1
                continue
            if verify_file_field_in_storage(equipment.image):
                skipped += 1
                continue

            rel_path = equipment.image.name
            local_path = os.path.join(local_root, rel_path)
            if not os.path.isfile(local_path):
                self.stdout.write(
                    self.style.WARNING(
                        f"Equipment {equipment.equipment_id} ({equipment.code}): "
                        f"not in S3 and no local file at {local_path}"
                    )
                )
                missing += 1
                continue

            if dry_run:
                self.stdout.write(
                    f"Would upload {local_path} -> S3 as {rel_path} "
                    f"(equipment {equipment.equipment_id})"
                )
                uploaded += 1
                continue

            try:
                with open(local_path, "rb") as fh:
                    content = fh.read()
                equipment.image.save(rel_path, ContentFile(content), save=True)
                if verify_file_field_in_storage(equipment.image):
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Uploaded equipment {equipment.equipment_id} ({equipment.code}): {rel_path}"
                        )
                    )
                    uploaded += 1
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f"Upload failed verification for equipment {equipment.equipment_id}: {rel_path}"
                        )
                    )
                    errors += 1
            except Exception as exc:
                self.stdout.write(
                    self.style.ERROR(
                        f"Failed equipment {equipment.equipment_id} ({equipment.code}): {exc}"
                    )
                )
                errors += 1

        self.stdout.write(
            f"Done. uploaded={uploaded} already_ok={skipped} missing_local={missing} errors={errors}"
            + (" (dry-run)" if dry_run else "")
        )
