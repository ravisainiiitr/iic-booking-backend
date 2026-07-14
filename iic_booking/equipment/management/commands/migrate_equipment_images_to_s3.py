"""
Upload equipment images from legacy local storage to S3.

Before production used S3 exclusively, equipment images were sometimes stored
on the app server under MEDIA_ROOT/equipment_images_local/. Re-upload any files
that exist locally but are missing from the image field's storage backend.

Usage (run on the production host / django container once):
  python manage.py migrate_equipment_images_to_s3 --dry-run
  python manage.py migrate_equipment_images_to_s3
"""

from __future__ import annotations

import os
from typing import Optional

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from iic_booking.equipment.image_utils import (
    local_equipment_image_path,
    normalize_storage_path,
    storage_path_candidates,
    verify_file_field_in_storage,
)
from iic_booking.equipment.models import Equipment


class Command(BaseCommand):
    help = "Upload legacy local equipment images to S3 (one-time after storage backend change)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be uploaded without writing to S3.",
        )

    def _find_local_file(self, rel_path: str) -> Optional[str]:
        """Return absolute path of a local backup for this stored relative path, if any."""
        candidates = []
        for key in storage_path_candidates(rel_path):
            candidates.append(local_equipment_image_path(key))
            candidates.append(os.path.join(settings.MEDIA_ROOT, key))
            candidates.append(os.path.join(settings.MEDIA_ROOT, "equipment_images_local", key))
            if key.startswith("media/"):
                candidates.append(local_equipment_image_path(key[6:]))
            else:
                candidates.append(local_equipment_image_path("media/" + key))
        seen = set()
        for path in candidates:
            if not path or path in seen:
                continue
            seen.add(path)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
        return None

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
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

            rel_path = normalize_storage_path(equipment.image.name) or equipment.image.name
            local_path = self._find_local_file(equipment.image.name)
            if not local_path:
                self.stdout.write(
                    self.style.WARNING(
                        f"Equipment {equipment.equipment_id} ({equipment.code}): "
                        f"not in S3 and no local file for {equipment.image.name}"
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
                # Save via field storage (S3 in production); keep a stable relative name.
                equipment.image.save(rel_path, ContentFile(content), save=True)
                equipment.refresh_from_db()
                if verify_file_field_in_storage(equipment.image):
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Uploaded equipment {equipment.equipment_id} ({equipment.code}): "
                            f"{equipment.image.name}"
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
